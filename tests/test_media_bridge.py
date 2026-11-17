import asyncio
import hashlib
import secrets
import unittest

try:
    from experiments.ws_turn_bridge import LoopbackGateway, LoopbackConnectProxy
except ModuleNotFoundError as exc:
    if exc.name == 'websockets':
        raise unittest.SkipTest('Install the bridge-test optional dependency') from None
    raise


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_binary_roundtrip_with_tcp_half_close_and_large_payload(self):
        received = asyncio.get_running_loop().create_future()
        async def target(reader, writer):
            digest = hashlib.sha256()
            try:
                while block := await reader.read(8192):
                    digest.update(block)
                    writer.write(block)
                    await writer.drain()
                writer.write(b"after-client-eof")
                await writer.drain()
                received.set_result(digest.hexdigest())
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(target, "127.0.0.1", 0)
        payload = bytes(range(256)) * 8192
        try:
            port = server.sockets[0].getsockname()[1]
            token = secrets.token_urlsafe(32)
            async with LoopbackGateway(port, token) as gateway:
                async with LoopbackConnectProxy(gateway.url, token, port) as proxy:
                    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
                    writer.write(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
                    await writer.drain()
                    self.assertIn(b"200 OK", await reader.readuntil(b"\r\n\r\n"))
                    async def send():
                        for offset in range(0,len(payload),4093):
                            writer.write(payload[offset:offset+4093])
                            await writer.drain()
                        writer.write_eof()
                    sender = asyncio.create_task(send())
                    reply = await asyncio.wait_for(reader.read(), 15)
                    await sender
                    writer.close()
                    await writer.wait_closed()
                    self.assertEqual(reply, payload + b"after-client-eof")
                    self.assertEqual(await received, hashlib.sha256(payload).hexdigest())
                    self.assertEqual(proxy.counts["errors"], 0)
        finally:
            server.close()
            await server.wait_closed()

    async def test_arbitrary_destination_is_rejected_before_gateway_connect(self):
        async with LoopbackConnectProxy("ws://127.0.0.1:9/turn", "x"*32, 12345) as proxy:
            r,w = await asyncio.open_connection("127.0.0.1", proxy.port)
            w.write(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
            await w.drain()
            self.assertIn(b"403 Forbidden", await r.read())
            self.assertEqual(proxy.counts["connections"], 0)
            w.close()
            await w.wait_closed()

    async def test_failed_gateway_auth_never_returns_connect_success(self):
        async with LoopbackGateway(12345, "x"*32) as gateway:
            async with LoopbackConnectProxy(gateway.url, "wrong-token", 12345) as proxy:
                r,w = await asyncio.open_connection("127.0.0.1", proxy.port)
                w.write(b"CONNECT 127.0.0.1:12345 HTTP/1.1\r\n\r\n")
                await w.drain()
                self.assertIn(b"502 Bad Gateway", await r.read())
                self.assertEqual(gateway.counts["connections"], 0)
                w.close()
                await w.wait_closed()

    def test_remote_gateway_cannot_be_used_by_loopback_experiment(self):
        with self.assertRaises(ValueError):
            LoopbackConnectProxy("wss://example.com/turn", "x"*32, 12345)
