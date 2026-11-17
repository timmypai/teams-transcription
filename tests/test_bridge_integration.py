"""Opt-in real WebRTC capture regression: all media stays on loopback."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    from experiments.probe_ws_turn_bridge import run
except ModuleNotFoundError as exc:
    if exc.name == 'websockets':
        raise unittest.SkipTest('Install the bridge-test optional dependency') from None
    raise

TURN = os.environ.get('MEETING_TEST_TURNSERVER') or shutil.which('turnserver')


@unittest.skipUnless(TURN, 'Local coturn fixture is not installed')
class RemoteCaptureRegression(unittest.IsolatedAsyncioTestCase):
    async def test_real_remote_audio_survives_bridge_and_is_not_silent(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'
            args = SimpleNamespace(turnserver=TURN,
                library_path=os.environ.get('MEETING_TEST_TURN_LIBRARY_PATH'),
                chromium=shutil.which('chromium'), output=str(output), quiet=True)
            self.assertEqual(await run(args), 0)
            result = json.loads(output.read_text())
            self.assertTrue(result['binary_echo_exact'])
            self.assertGreater(result['captured_rms'], .01)
            self.assertEqual(result['recorder'][0]['dropped'], 0)
            self.assertEqual(result['recorder'][0]['playout_errors'], 0)
            self.assertEqual(result['gateway_after_teardown']['active'], 0)
