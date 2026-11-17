"""Existing HTTP/ICE destination rules and source-proven dynamic Live media.

Official ChatGPT Live negotiates dynamic public media addresses, distinct from
HTTP host DNS. Only one authenticated, own-thread, accepted negotiation may
authorize its SDP. This local implementation has not had real service validation.
"""
import asyncio
import ipaddress
import re
import socket
import hashlib
from urllib.parse import urlsplit

from .b_guard import ExistingLiveGuard, OfficialSDPProof


class RuntimeLiveGuard:
    def __init__(self, meeting_id, allowed_hosts, *, resolver=socket.getaddrinfo, ice_servers=None):
        if (not meeting_id or not isinstance(allowed_hosts, (list, tuple)) or any(
                not isinstance(h, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", h)
                or ".." in h for h in allowed_hosts)):
            raise ValueError("existing_egress_policy_required")
        self.meeting_id = meeting_id
        self.allowed_hosts = tuple(allowed_hosts)
        # Empty is an explicit deny-all browser HTTP/WebSocket policy. It does
        # not authorize media; the own-start SDP proof below is still required.
        self.resolver = resolver
        self.ice_servers = ice_servers or []
        self.context = self.binding = self.handle = None
        self._answer_proof = None
        self._answer_authorized = False

    def adapter(self):
        return ExistingLiveGuard(self.install, self.validate, authorize_official_answer=self.authorize_official_answer)

    async def prepare(self):
        """Check configured ICE endpoints before a peer can gather candidates."""
        for server in self.ice_servers:
            urls = server.get("urls", [])
            urls = [urls] if isinstance(urls, str) else urls
            if not isinstance(urls, list):
                raise ValueError("response_ice_destination_denied")
            for url in urls:
                if not isinstance(url, str) or not re.fullmatch(r"(?:stuns?|turns?):[a-z0-9.-]+(?::[0-9]{1,5})?(?:\?transport=(?:udp|tcp))?", url):
                    raise ValueError("response_ice_destination_denied")
                parsed = urlsplit("https://" + url.split(":", 1)[1])
                if parsed.port is not None and not 1 <= parsed.port <= 65535:
                    raise ValueError("response_ice_destination_denied")
                await asyncio.to_thread(self._destination, "https://" + parsed.hostname)

    def _destination(self, url):
        from .security import validate_destination
        return validate_destination(url, self.allowed_hosts, resolver=self.resolver)

    async def install(self, context, binding):
        if self.context is not None or binding.meeting_id != self.meeting_id:
            raise RuntimeError("response_guard_binding_invalid")
        self.context, self.binding = context, binding
        # Service workers must be able to intercept WebSockets as well as HTTP.
        if not callable(getattr(context, "route_web_socket", None)):
            raise RuntimeError("response_websocket_guard_unavailable")

        async def request(route):
            try:
                await asyncio.to_thread(self._destination, route.request.url)
            except Exception:
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        async def websocket(route):
            try:
                await asyncio.to_thread(self._destination, route.url)
            except Exception:
                await route.close(code=1008, reason="destination denied")
                return
            route.connect_to_server()

        await context.route("**/*", request)
        await context.route_web_socket("**/*", websocket)
        self.handle = object()
        return self.handle

    def authorize_official_answer(self, handle, context, binding, proof):
        if (handle is not self.handle or context is not self.context or binding != self.binding
                or binding.provider != "codex-live" or not isinstance(proof, OfficialSDPProof)
                or proof.binding != binding or self._answer_authorized):
            return False
        self._answer_authorized = True
        self._answer_proof = proof
        return True

    def _sdp(self, sdp, kind):
        if not isinstance(sdp, str) or not 0 < len(sdp) <= 131072 or "\x00" in sdp:
            return False
        lines = sdp.splitlines()
        if len(lines) > 1024 or lines[0] != "v=0":
            return False
        media = [line.split() for line in lines if line.startswith("m=")]
        if (not media or any(len(m) < 4 or m[0] not in {"m=audio", "m=application"}
                             or m[2] not in {"UDP/TLS/RTP/SAVPF", "UDP/DTLS/SCTP"} for m in media)
                or not any(re.fullmatch(r"a=fingerprint:sha-256 (?:[0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2}", line) for line in lines)):
            return False
        candidates = [line for line in lines if line.startswith("a=candidate:")]
        if not candidates or len(candidates) > 256:
            return False
        candidate_addresses = set()
        for line in candidates:
            parts = line.split()
            if (len(parts) < 8 or parts[1] not in {"1", "2"} or parts[2].lower() not in {"udp", "tcp"}
                    or parts[6] != "typ" or parts[7] not in {"host", "srflx", "relay", "prflx"}):
                return False
            try:
                port = int(parts[5])
                priority = int(parts[3])
            except ValueError:
                return False
            if not 1 <= port <= 65535 or not 0 <= priority <= 4294967295:
                return False
            try:
                address = ipaddress.ip_address(parts[4])
            except ValueError:
                # Chromium may anonymize its own local host candidate as mDNS.
                # It is a local source, never a permitted remote destination.
                if (kind == "offer" and parts[7] == "host"
                        and re.fullmatch(r"[0-9a-fA-F-]{16,64}\.local", parts[4])):
                    continue
                return False
            if not 1 <= port <= 65535 or address.is_multicast or address.is_unspecified:
                return False
            if kind == "answer" and not address.is_global:
                return False
            candidate_addresses.add(str(address))
        for line in lines:
            if not line.startswith("c="):
                continue
            parts = line.split()
            if len(parts) != 3 or parts[:2] not in (["c=IN", "IP4"], ["c=IN", "IP6"]):
                return False
            try:
                address = ipaddress.ip_address(parts[2])
            except ValueError:
                return False
            if not address.is_unspecified and str(address) not in candidate_addresses:
                return False
        return True

    async def validate(self, handle, context, binding, sdp, kind):
        if (handle is not self.handle or context is not self.context or binding != self.binding
                or binding.meeting_id != self.meeting_id or kind not in {"offer", "answer"}):
            return False
        if kind == "answer":
            proof = self._answer_proof
            if (proof is None or proof.binding != binding
                    or not isinstance(sdp, str) or hashlib.sha256(sdp.encode()).hexdigest() != proof.sdp_sha256):
                return False
            # A negotiated answer is a single-use capability, including failures.
            self._answer_proof = None
        try:
            return await asyncio.to_thread(self._sdp, sdp, kind)
        except Exception:
            return False
