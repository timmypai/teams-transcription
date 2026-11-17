"""ICE configuration and credential-free, proxy-preserving TURN diagnostics."""
import asyncio
import base64
import concurrent.futures
import json
import os
import re
import secrets
import socket
import ssl
import struct
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .browser import Browser
from .storage import atomic_json, stamp


def load_ice_servers(path):
    """Read only an explicitly supplied file; never put its contents in config/logs."""
    if not path:
        return []
    try:
        with Path(path).open("rb") as file:
            data = file.read(65537)
        if len(data) > 65536:
            raise ValueError()
        servers = json.loads(data)
        if not isinstance(servers, list) or not 1 <= len(servers) <= 16:
            raise ValueError()
        for server in servers:
            if not isinstance(server, dict) or set(server) - {"urls", "username", "credential"}:
                raise ValueError()
            urls = server.get("urls")
            urls = [urls] if isinstance(urls, str) else urls
            if not isinstance(urls, list) or not 1 <= len(urls) <= 16:
                raise ValueError()
            for url in urls:
                if not isinstance(url, str) or not re.fullmatch(
                    r"(?:stuns?|turns?):(?:[A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])"
                    r"(?::[0-9]{1,5})?(?:\?transport=(?:tcp|udp))?", url):
                    raise ValueError()
                parsed = urlsplit("https://" + url.split(":", 1)[1])
                if parsed.port is not None and not 1 <= parsed.port <= 65535:
                    raise ValueError()
                if url.startswith(("turn:", "turns:")) and not all(
                    isinstance(server.get(k), str) and server[k] for k in ("username", "credential")):
                    raise ValueError()
            if any(not isinstance(server[k], str) for k in ("username", "credential") if k in server):
                raise ValueError()
        return servers
    except (OSError, ValueError, TypeError):
        # JSON/parser/browser exceptions can echo the secret-bearing document.
        raise ValueError("Invalid ICE servers file; supply RTCIceServer entries with TURN credentials") from None


def turn_reply(data, transaction):
    """Classify an unauthenticated Allocate reply without exposing nonce/realm/IP."""
    if data.startswith(b"HTTP/"):
        match = re.match(rb"HTTP/\d(?:\.\d)? ([0-9]{3})\b", data)
        return {"reply": "http", "http_status": int(match[1]) if match else None,
                "turn_protocol_reachable": False}
    if len(data) < 20:
        return {"reply": "eof" if not data else "incomplete", "turn_protocol_reachable": False}
    kind, size, cookie = struct.unpack("!HHI", data[:8])
    if cookie != 0x2112A442 or data[8:20] != transaction or size % 4 or len(data) < 20 + size:
        return {"reply": "invalid_stun", "turn_protocol_reachable": False}
    code = None
    pos = 20
    while pos + 4 <= 20 + size:
        attr, length = struct.unpack("!HH", data[pos:pos + 4])
        if pos + 4 + length > 20 + size:
            return {"reply": "invalid_stun", "turn_protocol_reachable": False}
        if attr == 9 and length >= 4:
            code = (data[pos + 6] & 7) * 100 + data[pos + 7]
        pos += 4 + ((length + 3) // 4) * 4
    # 401 is the expected TURN authentication challenge, not a login failure.
    return {"reply": "stun", "message_type": hex(kind), "stun_error_code": code,
            "turn_protocol_reachable": kind == 0x103 or (kind == 0x113 and code == 401)}


def probe_turn(host, port, tls, proxy_url, timeout=12):
    result = {"transport": "turns" if tls else "turn", "port": port,
              "turn_protocol_reachable": False}
    sock = None
    start = time.monotonic()
    try:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host) or not 1 <= port <= 65535:
            raise ValueError("Invalid relay hostname or port")
        if not proxy_url:
            result["error"] = "No inherited HTTPS proxy; this diagnostic does not attempt direct access"
            return result
        proxy = urlsplit(proxy_url)
        if proxy.scheme not in {"http", "https"} or not proxy.hostname:
            raise ValueError("Unsupported inherited HTTPS proxy")
        sock = socket.create_connection((proxy.hostname, proxy.port or (443 if proxy.scheme == "https" else 80)), timeout)
        if proxy.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=proxy.hostname)
        header = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        if proxy.username:
            user, password = unquote(proxy.username), unquote(proxy.password or "")
            auth = base64.b64encode(f"{user}:{password}".encode()).decode()
            header += f"Proxy-Authorization: Basic {auth}\r\n"
        sock.sendall((header + "\r\n").encode())
        response = bytearray()
        while not response.endswith(b"\r\n\r\n"):
            byte = sock.recv(1)
            if not byte or len(response) >= 16384:
                raise ConnectionError("Incomplete proxy response")
            response.extend(byte)
        status = re.match(rb"HTTP/\d(?:\.\d)? ([0-9]{3})\b", response)
        result["connect_status"] = int(status[1]) if status else None
        if result["connect_status"] != 200:
            return result
        if tls:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            result["tls_version"] = sock.version()
            result["tls_verified"] = True
        transaction = secrets.token_bytes(12)
        attr = struct.pack("!HHBBBB", 0x19, 4, 17, 0, 0, 0)
        sock.sendall(struct.pack("!HHI", 3, len(attr), 0x2112A442) + transaction + attr)
        response = bytearray()
        while len(response) < 16384:
            chunk = sock.recv(min(4096, 16384 - len(response)))
            if not chunk:
                break
            response.extend(chunk)
            if response.startswith(b"HTTP/") and b"\r\n" in response:
                break
            if len(response) >= 20 and not response.startswith(b"HTTP/"):
                length = struct.unpack("!H", response[2:4])[0]
                if len(response) >= 20 + length:
                    break
        result.update(turn_reply(response, transaction))
    except (OSError, ValueError) as exc:
        result["error_type"] = type(exc).__name__
        if isinstance(exc, ssl.SSLError):
            result["ssl_reason"] = exc.reason
        elif isinstance(exc, TimeoutError):
            result["error"] = "timeout"
        elif isinstance(exc, OSError):
            result["errno"] = exc.errno
    finally:
        if sock:
            sock.close()
        result["elapsed_seconds"] = round(time.monotonic() - start, 3)
    return result


NATIVE_ICE_CONTROL = r"""async ({audio,pool}) => {
  const pc = new RTCPeerConnection({iceServers:[],iceCandidatePoolSize:pool});
  let ctx;
  try {
    if (audio) {
      ctx = new AudioContext({sampleRate:24000});
      const d = ctx.createMediaStreamDestination();
      pc.addTrack(d.stream.getAudioTracks()[0],d.stream);
    } else pc.createDataChannel('control');
    const start = performance.now();
    await pc.setLocalDescription(await pc.createOffer());
    await Promise.race([new Promise(r => {
      if (pc.iceGatheringState === 'complete') return r();
      pc.addEventListener('icegatheringstatechange', () => { if(pc.iceGatheringState === 'complete') r(); });
    }),new Promise(r => setTimeout(r,5000))]);
    return {secure:isSecureContext, audio, pool, gathering:pc.iceGatheringState,
      elapsed_ms:Math.round(performance.now()-start),
      candidates:(pc.localDescription.sdp.match(/^a=candidate:/mg)||[]).length};
  } finally {pc.close(); if(ctx)await ctx.close();}
}"""


async def probe_media(args):
    result = {"tested_at": stamp(), "scope": "Unauthenticated TURN reachability; not admission or transcription",
              "relay_host": args.relay_host, "turn_credentials_sent": False, "media_sent": False}
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        result["relay_probes"] = list(pool.map(
            lambda tls: probe_turn(args.relay_host, args.relay_port, tls, proxy), [False, True]))
    async with Browser(args.chromium) as owner:
        async def control(secure, audio, pool):
            context = await owner.browser.new_context()
            try:
                page = await context.new_page()
                if secure:
                    await context.route("https://webrtc-control.test/**", lambda r: r.fulfill(
                        status=200, body="<html>Isolated ICE control</html>", content_type="text/html"))
                    await page.goto("https://webrtc-control.test/")
                return await page.evaluate(NATIVE_ICE_CONTROL, {"audio":audio,"pool":pool})
            finally:
                await context.close()
        result["native_ice_controls"] = await asyncio.gather(
            control(False, False, 0), control(False, True, 0),
            control(True, False, 0), control(True, True, 1))
    result["success"] = any(p["turn_protocol_reachable"] for p in result["relay_probes"])
    if result["success"]:
        result["diagnosis"] = "turn_reachable_authentication_and_media_still_unverified"
    elif any(p.get("reply") == "http" for p in result["relay_probes"]):
        result["diagnosis"] = "http_proxy_rejects_turn_protocol"
    else:
        result["diagnosis"] = "turn_unreachable_check_proxy_transport_and_endpoint"
    path = Path(args.output).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, result)
    from .cli import output
    output(result)
    return 0 if result["success"] else 1
