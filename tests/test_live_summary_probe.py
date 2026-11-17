"""Actual draft connector with injected RPC/peer; no model/browser/audio device."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from teams_transcription.b_guard import ExistingLiveGuard
from teams_transcription.live_summary_probe import LiveSummaryProbe


class LiveSummaryProbeTests(unittest.IsolatedAsyncioTestCase):
    def make_probe(self, *, mode="valid", account="chatgpt", timeout=.3, entries=None,
                   prior_minutes="", stop_closed=True):
        guard = ExistingLiveGuard(lambda context, binding: object(), lambda *args: True)
        probe = LiveSummaryProbe(None, "/tmp/live-summary-probe-unused", "meeting-one",
            entries if entries is not None else [{"id": "s1", "text": "預算十萬元。"}],
            prior_minutes=prior_minutes, guard=guard, timeout=timeout, stop_timeout=.02,
            nonce="a" * 32)
        probe._verify_pinned_binary = AsyncMock()
        probe.peer = AsyncMock()
        probe.peer.offer.return_value = "controlled-offer"
        probe.rpc = AsyncMock()
        probe.rpc.executable = "codex"
        probe.rpc.notifications = asyncio.Queue()
        probe.rpc.thread.return_value = "probe-thread"
        probe.rpc.process = SimpleNamespace(returncode=None)
        calls = []

        def notify(method, *, thread="probe-thread", **params):
            probe.rpc.notifications.put_nowait({"method": method,
                "params": {"threadId": thread, **params}})

        value = {"重點": [{"text": "預算十萬元。", "sources": ["s1"],
                        "owner": "", "date": ""}], "決策": [], "待辦": [], "未決問題": []}

        async def request(method, params):
            calls.append((method, params))
            if method == "account/read":
                return {"account": {"type": account}}
            if method == "thread/realtime/start":
                notify("thread/realtime/sdp", sdp="controlled-answer")
                if mode == "early_assistant":
                    notify("thread/realtime/transcript/delta", role="assistant", delta="startup greeting")
            elif method == "thread/realtime/appendText":
                text = json.dumps(value, ensure_ascii=False) + "\n" + probe.stream.marker
                if mode == "valid":
                    notify("thread/realtime/transcript/done", thread="foreign", role="assistant",
                           text="foreign data")
                    notify("thread/realtime/transcript/done", role="user", text="untrusted user")
                    notify("thread/realtime/transcript/delta", role="assistant", delta=text[:20])
                    notify("thread/realtime/transcript/done", role="assistant", text=text)
                elif mode == "handoff":
                    notify("thread/realtime/itemAdded", item={"type": "handoff_request",
                        "handoff_id": "h1", "input_transcript": "delegated"})
                elif mode == "ordinary":
                    notify("turn/started", turn={"id": "regular"})
                elif mode == "closed":
                    notify("thread/realtime/closed")
                elif mode == "invalid":
                    notify("thread/realtime/transcript/done", role="assistant", text="bad JSON\n" + probe.stream.marker)
                elif mode == "late_handoff":
                    notify("thread/realtime/transcript/done", role="assistant", text=text)
            elif method == "thread/realtime/stop":
                if mode == "late_handoff":
                    notify("thread/realtime/itemAdded", item={"type": "handoff_request"})
                if stop_closed:
                    notify("thread/realtime/closed")
            return {}

        probe.rpc.request.side_effect = request
        return probe, calls, notify

    async def test_real_connector_own_assistant_draft_and_separate_owned_stop(self):
        entries = [{"id": "s1", "text": "預算十萬元。"}]
        probe, calls, _ = self.make_probe(entries=entries, prior_minutes="上次仍待確認預算。")
        entries[0]["text"] = "mutated source"
        result = await probe.run()
        self.assertEqual(result["state"], "draft_ready")
        self.assertEqual(result["summary"]["重點"][0]["sources"], ["s1"])
        self.assertTrue(result["draft_only"])
        self.assertFalse(result["service_contract_verified"])
        self.assertFalse(result["provider_final"])
        self.assertFalse(result["no_background_admission_guaranteed"])
        self.assertEqual(result["provenance"]["criterion"], "application_sentinel")
        self.assertEqual(result["stop"], {"state": "stopped", "ack_received": True,
                                         "closed_received": True, "cleanup_state": "cleaned"})
        start = next(p for m, p in calls if m == "thread/realtime/start")
        self.assertEqual((start["model"], start["version"], start["outputModality"]),
                         ("gpt-live-1-codex", "v3", "audio"))
        self.assertFalse(start["includeStartupContext"])
        self.assertFalse(start["flushTranscriptTailOnSessionEnd"])
        self.assertTrue(start["clientManagedHandoffs"])
        self.assertNotIn(probe.stream.marker, start["prompt"])
        self.assertEqual(start["transport"], {"type": "webrtc", "sdp": "controlled-offer"})
        append = next(p for m, p in calls if m == "thread/realtime/appendText")
        self.assertEqual(set(append), {"threadId", "text", "role"})
        self.assertIn("上次仍待確認預算。", append["text"])
        self.assertIn("__prior_minutes__", append["text"])
        self.assertIn(probe.stream.marker, append["text"])
        self.assertIn("預算十萬元。", append["text"])
        self.assertNotIn("mutated source", append["text"])
        self.assertFalse(any(m in {"turn/start", "thread/realtime/appendSpeech"} for m, _ in calls))
        probe.peer.send_event.assert_not_awaited()
        self.assertTrue(probe.output.closed)
        self.assertFalse(probe.output.enabled)
        probe.rpc.close.assert_awaited_once()
        probe.peer.close.assert_awaited_once()
        with self.assertRaises(RuntimeError):
            await probe.run()

    async def test_handoff_or_regular_turn_fails_even_after_sentinel_before_stop_closed(self):
        for mode in ("handoff", "ordinary", "late_handoff"):
            with self.subTest(mode=mode):
                probe, calls, _ = self.make_probe(mode=mode)
                result = await probe.run()
                self.assertEqual(result["state"], "failed")
                self.assertEqual(result["failure_code"], "live_summary_background_route_observed")
                self.assertIsNone(result["summary"])
                self.assertFalse(any(m == "turn/start" for m, _ in calls))

    async def test_no_output_times_out_not_success(self):
        probe, _, _ = self.make_probe(mode="silent", timeout=.03)
        result = await probe.run()
        self.assertEqual(result["failure_code"], "live_summary_probe_timeout")
        self.assertIsNone(result["summary"])
        self.assertTrue(result["stop"]["closed_received"])

    async def test_invalid_json_or_unsolicited_close_fails(self):
        for mode in ("invalid", "closed"):
            with self.subTest(mode=mode):
                probe, _, _ = self.make_probe(mode=mode)
                result = await probe.run()
                self.assertEqual(result["state"], "failed")
                self.assertIsNone(result["summary"])

    async def test_missing_stop_closed_preserves_draft_and_unknown_lifecycle(self):
        probe, _, _ = self.make_probe(stop_closed=False)
        result = await probe.run()
        self.assertEqual(result["state"], "draft_ready")
        self.assertEqual(result["stop"]["state"], "timeout")
        self.assertTrue(result["stop"]["ack_received"])
        self.assertFalse(result["stop"]["closed_received"])
        self.assertFalse(result["provider_final"])

    async def test_api_key_account_rejected_without_live_start(self):
        probe, calls, _ = self.make_probe(account="apiKey")
        result = await probe.run()
        self.assertEqual(result["state"], "failed")
        self.assertFalse(any(m == "thread/realtime/start" for m, _ in calls))

    async def test_startup_assistant_without_injected_source_fails_before_input(self):
        probe, calls, _ = self.make_probe(mode="early_assistant")
        result = await probe.run()
        self.assertEqual(result["failure_code"], "live_summary_unexpected_startup_output")
        self.assertIsNone(result["summary"])
        self.assertFalse(any(m == "thread/realtime/appendText" for m, _ in calls))

    async def test_binary_mismatch_rejected_before_any_rpc(self):
        probe, calls, _ = self.make_probe()
        del probe._verify_pinned_binary
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"codex-cli wrong", b"")))
        with patch("teams_transcription.live_summary_probe.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=process)):
            result = await probe.run()
        self.assertEqual(result["failure_code"], "live_summary_binary_contract_mismatch")
        self.assertEqual(calls, [])
        probe.rpc.start.assert_not_awaited()

    async def test_audio_discard_and_no_speech_route(self):
        probe, _, _ = self.make_probe()
        await probe.output.write("meeting-one", b"\x01\x00" * 100, sample_rate=48000, channels=1)
        self.assertEqual(set(probe.output.__dict__), {"closed"})
        with self.assertRaises(RuntimeError):
            probe.output.start_output()
        with self.assertRaises(RuntimeError):
            await probe.speak("hello")

    def test_guard_empty_duplicate_and_combined_source_budget(self):
        with self.assertRaises(ValueError):
            LiveSummaryProbe(None, "/tmp/unused", "m", [], guard=None)
        guard = ExistingLiveGuard(lambda *args: object(), lambda *args: True)
        for entries, prior in (([], ""), ([{"id": "a", "text": "b"}] * 2, ""),
                               ([{"id": "__prior_minutes__", "text": "b"}], "prior"),
                               ([{"id": "__prior_minutes__", "text": "b"}], ""),
                               ([{"id": "a", "text": "字" * 20000}], ""),
                               ([], "字" * 20000)):
            with self.subTest(entries_count=len(entries), prior_bytes=len(prior.encode())):
                with self.assertRaises(ValueError):
                    LiveSummaryProbe(None, "/tmp/unused", "m", entries, guard=guard, prior_minutes=prior)

    def test_invalid_options_rejected_before_creating_rpc_or_browser(self):
        guard = ExistingLiveGuard(lambda *args: object(), lambda *args: True)
        options = ({"nonce": "x" * 32}, {"nonce": 1}, {"max_output_bytes": 1},
                   {"max_source_bytes": 131072}, {"timeout": float("nan")})
        with patch("teams_transcription.b_live.CodexRPC") as rpc:
            for option in options:
                with self.subTest(option=option), self.assertRaises(ValueError):
                    LiveSummaryProbe(None, "/tmp/unused", "m", [{"id": "a", "text": "b"}],
                                     guard=guard, **option)
            with self.assertRaises(ValueError):
                LiveSummaryProbe(None, "/tmp/unused", "m", [{"id": "x" * 201, "text": "b"}], guard=guard)
            rpc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
