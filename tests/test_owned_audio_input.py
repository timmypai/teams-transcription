"""Sandboxed, route-fulfilled media tests; no Teams, model, or hardware access."""
from contextlib import asynccontextmanager
import unittest

from teams_transcription.browser import Browser, MeetingBrowser


OWNED_DEVICE_ID = 'teams-assistant-owned-audio'
TEAMS_HOSTS = ('teams.microsoft.com', 'teams.cloud.microsoft', 'teams.live.com')


class OwnedAudioInputTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def meeting_page(self, *, host='teams.microsoft.com', response_mode=True):
        async def discard_audio(_packet):
            self.fail('The local input fixture must not capture meeting audio')

        async def discard_event(_kind, _data):
            pass

        async with Browser() as runtime:
            meeting = MeetingBrowser(runtime.browser, discard_audio, discard_event,
                                     capture_audio=False, response_mode=response_mode)
            try:
                # Open without navigating so every subsequent request is intercepted.
                await meeting.open()
                await meeting.context.route('**/*', lambda route: route.fulfill(
                    status=200, content_type='text/html', body='''<!doctype html>
                    <title>Local owned-input fixture</title>
                    <script>
                    // Exercise document-init availability, before application setup.
                    window.initialDevices = navigator.mediaDevices.enumerateDevices();
                    </script>'''))
                await meeting.page.goto('https://' + host + '/owned-input-fixture',
                                        wait_until='domcontentloaded')
                yield meeting
            finally:
                await meeting.close()

    async def test_owned_input_is_available_at_document_start_and_muted_live(self):
        for host in TEAMS_HOSTS:
            with self.subTest(host=host):
                async with self.meeting_page(host=host) as meeting:
                    result = await meeting.page.evaluate('''async deviceId => {
                      const devices = await initialDevices;
                      const stream = await navigator.mediaDevices.getUserMedia({
                        audio: {deviceId: {exact: deviceId}}, video: false});
                      try {
                        const track = stream.getAudioTracks()[0];
                        const context = new AudioContext();
                        try {
                          const source = context.createMediaStreamSource(stream);
                          return {
                            inputs: devices.filter(d => d.kind === 'audioinput').map(
                              d => ({id: d.deviceId, label: d.label})),
                            stream: stream instanceof MediaStream,
                            audioTrack: track instanceof MediaStreamTrack && track.kind === 'audio',
                            audioCount: stream.getAudioTracks().length,
                            videoCount: stream.getVideoTracks().length,
                            state: track.readyState, enabled: track.enabled,
                            usable: source instanceof MediaStreamAudioSourceNode,
                            microphonePermission: (await navigator.permissions.query(
                              {name: 'microphone'})).state
                          };
                        } finally { await context.close(); }
                      } finally { stream.getTracks().forEach(track => track.stop()); }
                    }''', OWNED_DEVICE_ID)
                    self.assertEqual(len(result['inputs']), 1)
                    self.assertEqual(result['inputs'][0]['id'], OWNED_DEVICE_ID)
                    self.assertTrue(result['stream'])
                    self.assertTrue(result['audioTrack'])
                    self.assertTrue(result['usable'])
                    self.assertEqual(result['audioCount'], 1)
                    self.assertEqual(result['videoCount'], 0)
                    self.assertEqual(result['state'], 'live')
                    self.assertFalse(result['enabled'])
                    self.assertNotEqual(result['microphonePermission'], 'granted')

    async def test_unprocessed_capture_preferences_return_a_usable_owned_input(self):
        async with self.meeting_page() as meeting:
            result = await meeting.page.evaluate('''async deviceId => {
              const stream = await navigator.mediaDevices.getUserMedia({audio: {
                deviceId: {exact: deviceId},
                autoGainControl: false, echoCancellation: false, noiseSuppression: false,
                googAutoGainControl: false, googAutoGainControl2: false,
                googHighpassFilter: false, googNoiseSuppression: false,
                googTypingNoiseDetection: false}});
              const context = new AudioContext();
              try {
                const track = stream.getAudioTracks()[0];
                const source = context.createMediaStreamSource(stream);
                return {state: track.readyState, enabled: track.enabled,
                  usable: source instanceof MediaStreamAudioSourceNode,
                  settings: ['autoGainControl','echoCancellation','noiseSuppression'].map(
                    key => track.getSettings()[key])};
              } finally {
                stream.getTracks().forEach(track => track.stop());
                await context.close();
              }
            }''', OWNED_DEVICE_ID)
            self.assertEqual(result, {'state': 'live', 'enabled': False,
                                     'usable': True, 'settings': [False, False, False]})

    async def test_processing_preferences_do_not_claim_unsupported_required_features(self):
        async with self.meeting_page() as meeting:
            for feature in ('autoGainControl', 'echoCancellation', 'noiseSuppression'):
                with self.subTest(feature=feature):
                    result = await meeting.page.evaluate('''async feature => {
                      const settings = [];
                      for (const value of [{exact: false}, {ideal: true}, true]) {
                        const stream = await navigator.mediaDevices.getUserMedia({
                          audio: {[feature]: value}});
                        try {
                          const track = stream.getAudioTracks()[0];
                          settings.push({value: track.getSettings()[feature],
                            capabilities: track.getCapabilities()[feature],
                            enabled: track.enabled});
                        } finally { stream.getTracks().forEach(track => track.stop()); }
                      }
                      try {
                        const stream = await navigator.mediaDevices.getUserMedia({
                          audio: {[feature]: {exact: true}}});
                        stream.getTracks().forEach(track => track.stop());
                        return {settings, requiredError: null};
                      } catch (error) {
                        return {settings, requiredError: error.name, constraint: error.constraint};
                      }
                    }''', feature)
                    self.assertEqual(result, {
                        'settings': [{'value': False, 'capabilities': [False], 'enabled': False}] * 3,
                        'requiredError': 'OverconstrainedError', 'constraint': feature})

    async def test_camera_and_unknown_exact_devices_are_denied(self):
        async with self.meeting_page() as meeting:
            requests = (
                {'audio': False, 'video': True},
                {'audio': True, 'video': True},
                {'audio': {'deviceId': {'exact': OWNED_DEVICE_ID}}, 'video': True},
                {'audio': {'deviceId': {'exact': 'not-an-owned-input'}}, 'video': False},
                {'audio': {'deviceId': {'exact': ['not-an-owned-input']}}, 'video': False},
                {'audio': False, 'video': False},
            )
            for constraints in requests:
                with self.subTest(constraints=constraints):
                    result = await meeting.page.evaluate('''async constraints => {
                      try {
                        const stream = await navigator.mediaDevices.getUserMedia(constraints);
                        stream.getTracks().forEach(track => track.stop());
                        return {denied: false};
                      } catch (error) { return {denied: true}; }
                    }''', constraints)
                    self.assertTrue(result['denied'])

    async def test_stopping_one_request_does_not_end_other_or_future_streams(self):
        async with self.meeting_page() as meeting:
            result = await meeting.page.evaluate('''async deviceId => {
              const streams = [];
              const request = async constraints => {
                const stream = await navigator.mediaDevices.getUserMedia(constraints);
                streams.push(stream);
                return stream.getAudioTracks()[0];
              };
              try {
                const first = await request({audio: true});
                const second = await request({audio: {deviceId: {exact: deviceId}}});
                first.stop();
                const third = await request({audio: true, video: false});
                return {first: first.readyState, second: second.readyState,
                  third: third.readyState,
                  distinct: new Set([first.id, second.id, third.id]).size,
                  muted: [first, second, third].every(track => !track.enabled)};
              } finally { streams.forEach(s => s.getTracks().forEach(t => t.stop())); }
            }''', OWNED_DEVICE_ID)
            self.assertEqual(result, {'first': 'ended', 'second': 'live',
                                     'third': 'live', 'distinct': 3, 'muted': True})

    async def test_normal_offer_answer_exposes_selectable_muted_sender(self):
        async with self.meeting_page() as meeting:
            try:
                result = await meeting.page.evaluate('''async deviceId => {
                  window.fixturePeers = [];
                  window.fixtureStream = await navigator.mediaDevices.getUserMedia({
                    audio: {deviceId: {exact: deviceId}}, video: false});
                  const local = new RTCPeerConnection({iceServers: []});
                  const remote = new RTCPeerConnection({iceServers: []});
                  fixturePeers.push(local, remote);
                  const track = fixtureStream.getAudioTracks()[0];
                  const sender = local.addTrack(track, fixtureStream);
                  await local.setLocalDescription(await local.createOffer());
                  await remote.setRemoteDescription(local.localDescription);
                  await remote.setLocalDescription(await remote.createAnswer());
                  await local.setRemoteDescription(remote.localDescription);
                  const transceiver = local.getTransceivers().find(t => t.sender === sender);
                  return {direction: transceiver.currentDirection,
                    state: sender.track.readyState, enabled: sender.track.enabled,
                    senderCount: local.getSenders().length,
                    peerIndex: __meeting.peers.indexOf(local),
                    senderIndex: local.getSenders().indexOf(sender)};
                }''', OWNED_DEVICE_ID)
                self.assertIn(result['direction'], ('sendonly', 'sendrecv'))
                self.assertEqual(result['state'], 'live')
                self.assertFalse(result['enabled'])
                self.assertEqual(result['senderCount'], 1)
                frame, peer_index, sender_index = await meeting.response_sender('local-owned-input')
                self.assertEqual(frame, meeting.page.main_frame)
                self.assertEqual(peer_index, result['peerIndex'])
                self.assertEqual(sender_index, result['senderIndex'])
            finally:
                await meeting.page.evaluate('''() => {
                  (window.fixturePeers || []).forEach(peer => peer.close());
                  window.fixtureStream?.getTracks().forEach(track => track.stop());
                }''')

    async def test_rx_only_and_non_teams_origins_do_not_gain_owned_input(self):
        cases = (
            ('teams.microsoft.com', False),
            ('teams.cloud.microsoft', False),
            ('teams.live.com', False),
            ('example.test', True),
            ('teams.microsoft.com.example.test', True),
            ('sub.teams.microsoft.com', True),
        )
        for host, response_mode in cases:
            with self.subTest(host=host, response_mode=response_mode):
                async with self.meeting_page(host=host, response_mode=response_mode) as meeting:
                    result = await meeting.page.evaluate('''async deviceId => ({
                      ownedInputs: (await initialDevices).filter(
                        d => d.kind === 'audioinput' && d.deviceId === deviceId).length,
                      microphonePermission: (await navigator.permissions.query(
                        {name: 'microphone'})).state,
                      cameraPermission: (await navigator.permissions.query(
                        {name: 'camera'})).state
                    })''', OWNED_DEVICE_ID)
                    self.assertEqual(result['ownedInputs'], 0)
                    self.assertNotEqual(result['microphonePermission'], 'granted')
                    self.assertNotEqual(result['cameraPermission'], 'granted')


if __name__ == '__main__':
    unittest.main()
