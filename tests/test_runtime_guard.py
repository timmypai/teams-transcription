import socket
import unittest
from unittest.mock import AsyncMock

from teams_transcription.b_guard import LiveBinding, OfficialSDPEnvelope
from teams_transcription.runtime_guard import RuntimeLiveGuard


def resolver(host, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8" if host == "live.example.test" else "127.0.0.1", 443))]


def sdp(address="8.8.8.8"):
    return "\r\n".join(["v=0", "o=- 1 1 IN IP4 0.0.0.0", "s=-", "t=0 0",
        "m=audio 9 UDP/TLS/RTP/SAVPF 111", "c=IN IP4 0.0.0.0",
        "a=fingerprint:sha-256 " + ":".join(["AA"]*32),
        "a=candidate:1 1 udp 123 " + address + " 3456 typ host", ""])


class Context:
    async def route(self, pattern, callback):
        self.http = callback

    async def route_web_socket(self, pattern, callback):
        self.websocket = callback


class RuntimeGuardTests(unittest.IsolatedAsyncioTestCase):
    async def setup_guard(self):
        guard = RuntimeLiveGuard("meeting-a", ["live.example.test"], resolver=resolver)
        context = Context()
        binding = LiveBinding("meeting-a", "thread-a")
        handle = await guard.install(context, binding)
        return guard, context, binding, handle

    async def test_every_http_and_websocket_destination_uses_existing_policy(self):
        guard, context, *_ = await self.setup_guard()
        route = AsyncMock()
        route.request.url = "https://live.example.test/path"
        await context.http(route)
        route.continue_.assert_awaited_once()
        route.abort.assert_not_called()
        denied = AsyncMock()
        denied.request.url = "https://foreign.example.test/path"
        await context.http(denied)
        denied.abort.assert_awaited_once()
        denied.continue_.assert_not_called()
        websocket = AsyncMock()
        websocket.url = "wss://live.example.test/events"
        # Playwright connect_to_server is synchronous.
        from unittest.mock import Mock
        websocket.connect_to_server = Mock()
        await context.websocket(websocket)
        websocket.connect_to_server.assert_called_once()
        websocket.close.assert_not_called()
        websocket.url = "wss://127.0.0.1/events"
        await context.websocket(websocket)
        websocket.close.assert_awaited_once()

    async def test_explicit_deny_all_http_policy_still_blocks_network_and_unproven_media(self):
        guard = RuntimeLiveGuard("meeting-a", [], resolver=lambda *a, **k: self.fail("deny-all must not resolve"))
        context, binding = Context(), LiveBinding("meeting-a", "thread-a")
        handle = await guard.install(context, binding)
        route = AsyncMock()
        route.request.url = "https://live.example.test/path"
        await context.http(route)
        route.abort.assert_awaited_once_with("blockedbyclient")
        route.continue_.assert_not_called()
        websocket = AsyncMock()
        websocket.url = "wss://live.example.test/events"
        await context.websocket(websocket)
        websocket.close.assert_awaited_once_with(code=1008, reason="destination denied")
        websocket.connect_to_server.assert_not_called()
        self.assertFalse(await guard.validate(handle, context, binding, sdp(), "answer"))

    async def authorized_guard(self, description):
        guard = RuntimeLiveGuard("meeting-a", ["live.example.test"], resolver=resolver)
        adapter = guard.adapter()
        context, binding = Context(), LiveBinding("meeting-a", "thread-a")
        await adapter.install(context, binding)
        source, attempt = object(), "a"*32
        params = {"threadId":"thread-a", "model":"gpt-live-1-codex", "version":"v3",
            "transport":{"type":"webrtc"}}
        adapter.begin_official_start(context, binding, source, "chatgpt", attempt, params)
        adapter.acknowledge_official_start(context, binding, source, attempt)
        await adapter.authorize_official_answer(context, binding,
            OfficialSDPEnvelope(source, attempt, "thread-a", description))
        return guard, adapter, context, binding

    async def test_source_proven_dynamic_public_media_does_not_require_host_DNS_match(self):
        guard, adapter, context, binding = await self.authorized_guard(sdp("9.9.9.9"))
        guard.resolver = lambda *a, **k: (_ for _ in ()).throw(AssertionError("SDP must not DNS-resolve"))
        self.assertTrue(await guard.validate(adapter.handle, context, binding, sdp("9.9.9.9"), "answer"))
        # One negotiated answer is single-use and cannot authorize a replay.
        self.assertFalse(await guard.validate(adapter.handle, context, binding, sdp("9.9.9.9"), "answer"))
        self.assertTrue(await guard.validate(adapter.handle, context, binding, sdp("10.0.0.2"), "offer"))
        local_mdns = sdp("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.local")
        self.assertTrue(await guard.validate(adapter.handle, context, binding, local_mdns, "offer"))

    async def test_proof_absence_mismatch_private_multicast_bad_ports_types_are_rejected(self):
        guard, context, binding, handle = await self.setup_guard()
        self.assertFalse(await guard.validate(handle, context, binding, sdp(), "answer"))
        bad_descriptions = (sdp("10.0.0.2"), sdp("127.0.0.1"), sdp("224.0.0.1"),
            sdp("unknown.example.test"), sdp("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.local"),
            sdp().replace("3456 typ", "0 typ"), sdp().replace("3456 typ", "65536 typ"),
            sdp().replace("udp 123", "ftp 123"), sdp().replace("typ host", "typ arbitrary"),
            sdp().replace("m=audio", "m=video"), sdp().replace("a=fingerprint", "a=unknown"),
            sdp().replace("a=candidate:", "a=unknown:"))
        for bad in bad_descriptions:
            guard, adapter, context, binding = await self.authorized_guard(bad)
            self.assertFalse(await guard.validate(adapter.handle, context, binding, bad, "answer"))
        guard, adapter, context, binding = await self.authorized_guard(sdp())
        self.assertFalse(await guard.validate(adapter.handle, context, binding, sdp("9.9.9.9"), "answer"))
        self.assertFalse(await guard.validate(adapter.handle, Context(), binding, sdp(), "answer"))
        self.assertFalse(await guard.validate(adapter.handle, context, LiveBinding("meeting-b", "thread-a"), sdp(), "answer"))

    async def test_official_source_requires_account_own_start_ACK_thread_attempt_and_context(self):
        guard = RuntimeLiveGuard("meeting-a", ["live.example.test"], resolver=resolver)
        adapter = guard.adapter()
        context, binding = Context(), LiveBinding("meeting-a", "thread-a")
        await adapter.install(context, binding)
        source, attempt = object(), "a"*32
        params = {"threadId":"thread-a", "model":"gpt-live-1-codex", "version":"v3",
            "transport":{"type":"webrtc"}}
        with self.assertRaises(RuntimeError):
            adapter.begin_official_start(context, binding, source, "apiKey", attempt, params)
        with self.assertRaises(RuntimeError):
            adapter.begin_official_start(context, binding, source, "chatgpt", attempt, {**params,"threadId":"foreign"})
        adapter.begin_official_start(context, binding, source, "chatgpt", attempt, params)
        envelope = OfficialSDPEnvelope(source, attempt, "thread-a", sdp())
        with self.assertRaises(RuntimeError):
            await adapter.authorize_official_answer(context, binding, envelope)  # no ACK
        adapter.acknowledge_official_start(context, binding, source, attempt)
        for bad in (OfficialSDPEnvelope(object(), attempt, "thread-a", sdp()),
                    OfficialSDPEnvelope(source, "b"*32, "thread-a", sdp()),
                    OfficialSDPEnvelope(source, attempt, "foreign", sdp())):
            with self.assertRaises(RuntimeError):
                await adapter.authorize_official_answer(context, binding, bad)
        with self.assertRaises(RuntimeError):
            await adapter.authorize_official_answer(Context(), binding, envelope)
        await adapter.authorize_official_answer(context, binding, envelope)
        with self.assertRaises(RuntimeError):
            await adapter.authorize_official_answer(context, binding, envelope)
        self.assertTrue(await guard.validate(adapter.handle, context, binding, sdp(), "answer"))

    async def test_no_policy_or_private_DNS_or_unknown_ICE_is_fail_closed(self):
        with self.assertRaises(ValueError):
            RuntimeLiveGuard("meeting-a", None)
        with self.assertRaises(ValueError):
            RuntimeLiveGuard("meeting-a", ["*.example.test"])
        guard = RuntimeLiveGuard("meeting-a", ["live.example.test"], resolver=resolver,
            ice_servers=[{"urls":"turn:unknown.example.test:3478?transport=udp"}])
        with self.assertRaises(Exception):
            await guard.prepare()
        private = RuntimeLiveGuard("meeting-a", ["private.example.test"], resolver=resolver)
        context, binding = Context(), LiveBinding("meeting-a", "thread-a")
        handle = await private.install(context, binding)
        with self.assertRaises(Exception):
            private._destination("https://private.example.test")


if __name__ == "__main__":
    unittest.main()
