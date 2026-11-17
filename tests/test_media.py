import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from teams_transcription.media import load_ice_servers, probe_turn, turn_reply


class MediaTests(unittest.TestCase):
    def test_expected_turn_challenge_is_reachability_not_http_success(self):
        tx = b"test-transac"
        attr = struct.pack("!HHBBBB", 9, 4, 0, 0, 4, 1)
        response = struct.pack("!HHI", 0x113, len(attr), 0x2112A442) + tx + attr
        result = turn_reply(response, tx)
        self.assertTrue(result["turn_protocol_reachable"])
        self.assertEqual(result["stun_error_code"], 401)
        for data, ident in [(response[:-1], tx), (response, b"wrong-transa"),
                            (b"HTTP/1.1 200 OK\r\n", tx), (b"HTTP/1.1 400 Bad Request\r\n", tx)]:
            self.assertFalse(turn_reply(data, ident)["turn_protocol_reachable"])

    def test_missing_proxy_never_falls_back_to_direct_connection(self):
        with patch("socket.create_connection") as connect:
            result = probe_turn("relay.example.test", 443, True, None)
        connect.assert_not_called()
        self.assertFalse(result["turn_protocol_reachable"])

    def test_invalid_ice_file_does_not_echo_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ice.json"
            for data in ['{"private-secret":', '[{"urls":"https://private-secret"}]',
                         '[{"urls":"turn:relay.test:99999","credential":"private-secret","username":"u"}]',
                         '[{"urls":"turn:relay.test:443"}]']:
                path.write_text(data)
                with self.assertRaises(ValueError) as raised:
                    load_ice_servers(path)
                self.assertNotIn("private-secret", str(raised.exception))


