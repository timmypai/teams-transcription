"""Controlled UI/track fixtures. These do not assert admission to a real meeting."""
import asyncio
import base64
import math
import json
import struct
import unittest
from unittest.mock import AsyncMock, Mock
from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
from teams_transcription.browser import Browser, MeetingBrowser, LivePeer, PEER_JS


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = Browser()
        await self.runtime.__aenter__()
        self.packets = []
        self.events = []
        async def packet(p):
            self.packets.append(p)
        async def event(kind, data):
            self.events.append(kind)
        self.meeting = MeetingBrowser(self.runtime.browser, packet, event)
        await self.meeting.open()

    async def asyncTearDown(self):
        await self.meeting.close()
        await self.runtime.__aexit__()

    async def test_host_clock_mapping_strips_forged_assertions_when_clock_or_times_invalid(self):
        import time
        frame = self.meeting.page.main_frame
        await self.meeting._start_capture_frame(frame, deadline=time.monotonic()+1)
        calibration = self.meeting.capture_clocks[frame]
        now = await frame.evaluate("() => performance.now()")
        packet = {"data":"AAA=", "frames":1, "start_frame":0, "sample_rate":24000,
            "capture_clock_id":calibration["id"], "capture_start_ms":now-1/24,
            "capture_end_ms":now, "source_clock":"monotonic",
            "source_started_at":123456, "source_ended_at":123457,
            "source_document":"page-forged-document"}
        bad_values = ({"capture_clock_id":"wrong"}, {"capture_clock_id":None},
            {"capture_start_ms":None}, {"capture_start_ms":-1},
            {"capture_start_ms":now+1}, {"capture_end_ms":float("inf")},
            {"capture_end_ms":now+1000}, {"capture_start_ms":now-1000})
        for invalid in bad_values:
            await frame.evaluate("p => window.meetingAudio(p)", {**packet, **invalid})
            actual = self.packets[-1]
            self.assertEqual(actual["data"], packet["data"])
            self.assertEqual(actual["frames"], 1)
            for name in ("source_clock", "source_started_at", "source_ended_at"):
                self.assertNotIn(name, actual)
        await frame.evaluate("p => window.meetingAudio(p)", packet)
        actual = self.packets[-1]
        self.assertEqual(actual["source_clock"], "monotonic")
        self.assertNotEqual(actual["source_started_at"], packet["source_started_at"])
        self.assertAlmostEqual(actual["source_ended_at"]-actual["source_started_at"], 1/24000, places=6)
        self.assertIs(actual['source_document'], calibration['document'])
        await frame.evaluate("() => { __meeting.capture_clock_id='new-document-generation'; }")
        await frame.evaluate("p => window.meetingAudio(p)", packet)
        self.assertNotIn('source_clock', self.packets[-1])
        self.assertNotIn('source_document', self.packets[-1])

    async def test_navigation_reissues_host_document_identity_and_rejects_old_packet(self):
        import time
        frame = self.meeting.page.main_frame
        await self.meeting._start_capture_frame(frame, deadline=time.monotonic()+1)
        first = self.meeting.capture_clocks[frame]
        await frame.goto('data:text/html,<p>New capture document</p>')
        await self.meeting._start_capture_frame(frame, deadline=time.monotonic()+1)
        second = self.meeting.capture_clocks[frame]
        self.assertIsNot(first['document'], second['document'])
        now = await frame.evaluate("() => performance.now()")
        packet = {"data":"AAA=", "frames":1, "start_frame":0, "sample_rate":24000,
            "capture_clock_id":first["id"], "capture_start_ms":now-1/24,
            "capture_end_ms":now, "source_document":"forged"}
        await frame.evaluate("p => meetingAudio(p)", packet)
        self.assertNotIn('source_document', self.packets[-1])
        self.assertNotIn('source_clock', self.packets[-1])
        await frame.evaluate("p => meetingAudio(p)", {**packet, "capture_clock_id":second["id"]})
        self.assertIs(self.packets[-1]['source_document'], second['document'])
        self.assertEqual(self.packets[-1]['source_clock'], 'monotonic')

    async def test_foreign_page_cannot_supply_observation_identity(self):
        foreign = await self.meeting.context.new_page()
        try:
            with self.assertRaisesRegex(PlaywrightError, 'capture_context_mismatch'):
                await foreign.evaluate("""() => meetingAudio({
                    data:'AAA=',sample_rate:24000,frames:1,start_frame:0,
                    source_document:'forged',capture_clock_id:__meeting.capture_clock_id
                })""")
            self.assertEqual(self.packets, [])
        finally:
            await foreign.close()

    async def test_teams_prejoin_iframe_mute_admit_leave(self):
        page = self.meeting.page
        await page.set_content("<iframe id='meeting'></iframe>")
        frame = page.frames[1]
        await frame.set_content('''<input placeholder="Enter your name"><button role="switch" aria-label="Microphone" aria-checked="true" onclick="this.setAttribute('aria-checked','false')">Microphone</button><button onclick="this.textContent='Leave'; document.querySelector('#people').hidden=false">Join now</button><button id="people" hidden>Participants</button>''')
        await self.meeting.join("teams", "Test Recorder", 3, lambda: False)
        self.assertEqual(await frame.get_by_placeholder("Enter your name").input_value(), "Test Recorder")
        self.assertEqual(await frame.get_by_role("switch").get_attribute("aria-checked"), "false")
        self.assertTrue(await self.meeting.admitted())
        self.assertEqual(self.events, ["joining", "connecting", "joined"])

    async def test_waiting_room_stop_and_timeout(self):
        await self.meeting.page.set_content('<button onclick="this.remove()">Join now</button><p role="status">Waiting for admission</p>')
        with self.assertRaises(TimeoutError):
            await self.meeting.join("teams", "Test", .7, lambda: False)
        with self.assertRaises(InterruptedError):
            await self.meeting.join("teams", "Test", 10, lambda: True)

    async def test_connecting_and_failure_are_not_admission(self):
        await self.meeting.page.set_content("<p role='alert'>Sorry, we couldn't connect you.</p><button>Rejoin call</button>")
        with self.assertRaisesRegex(RuntimeError, 'could not connect the call'):
            await self.meeting.join('teams', 'Test', 2, lambda: False)
        self.assertNotIn('joined', self.events)
        self.assertNotIn('waiting_for_admission', self.events)
        self.assertIn('meeting_join_diagnostics', self.events)

    async def test_lobby_leave_button_is_not_admission(self):
        await self.meeting.page.set_content('<p role="status">Waiting for someone to let you in</p><button>Leave</button>')
        with self.assertRaises(TimeoutError):
            await self.meeting.join('teams', 'Test', .2, lambda: False)
        self.assertNotIn('joined', self.events)
        self.assertIn('waiting_for_admission', self.events)

    async def test_leave_without_in_meeting_controls_never_admits(self):
        await self.meeting.page.set_content('<button>Leave</button>')
        self.assertFalse(await self.meeting.admitted())
        with self.assertRaises(TimeoutError):
            await self.meeting.join('teams', 'Test', .15, lambda: False)
        self.assertNotIn('joined', self.events)

    async def test_capture_lifecycle_distinguishes_end_removal_and_transient_ui_loss(self):
        self.meeting._join_platform = 'teams'
        for markup,expected in (
                ('<p role=\"status\">Meeting has ended</p>','confirmed_end'),
                ('<p role=\"alert\">You were removed</p>','meeting_removed'),
                ('<p role=\"status\">Connecting</p>','connecting'),
                ('<p>Temporary document</p>','unrecognized'),
                ('<button>Leave</button><button>Chat</button><div role=\"log\">Meeting has ended</div>','joined')):
            with self.subTest(expected=expected):
                await self.meeting.page.set_content(markup)
                self.assertEqual(await self.meeting.capture_lifecycle(),expected)

    async def test_chat_and_captions_cannot_supply_lifecycle_blockers(self):
        for message in ('Waiting in the lobby', 'Meeting has ended',
                        "Sorry, we couldn't connect you", 'Sign in to join'):
            with self.subTest(message=message):
                await self.meeting.page.set_content(
                    '<button>Leave</button><button>Participants</button>'
                    '<section role="log"><div role="status">' + message + '</div></section>'
                    '<div role="dialog"><section data-tid="closed-caption">' + message + '</section></div>')
                self.events.clear()
                await self.meeting.join('teams', 'Test', .3, lambda: False)
                self.assertTrue(await self.meeting.admitted())
                self.assertEqual(self.events, ['joining', 'joined'])

    async def test_mixed_dialog_preserves_real_lobby_veto(self):
        await self.meeting.page.set_content(
            '<button>Leave</button><button>Chat</button>'
            '<div role="dialog"><section role="log">Meeting has ended</section>'
            '<p>Someone will let you in when the meeting starts.</p></div>')
        self.assertFalse(await self.meeting.admitted())
        with self.assertRaises(TimeoutError):
            await self.meeting.join('teams', 'Test', .2, lambda: False)
        self.assertIn('waiting_for_admission', self.events)
        self.assertNotIn('joined', self.events)

    async def test_admission_controls_cannot_be_combined_across_frames(self):
        await self.meeting.page.set_content('<button>Leave</button><iframe></iframe>')
        await self.meeting.page.frames[1].set_content('<button>Chat</button>')
        self.assertFalse(await self.meeting.admitted())
        await self.meeting.page.frames[1].set_content('<button>Leave</button><button>Chat</button>')
        self.assertTrue(await self.meeting.admitted())

    async def test_stalled_ui_obeys_admission_timeout_and_stop(self):
        async def stalled(*args, **kwargs):
            await asyncio.sleep(60)
        self.meeting._join = AsyncMock(side_effect=stalled)
        with self.assertRaisesRegex(TimeoutError, 'admission timed out'):
            await asyncio.wait_for(self.meeting.join('teams', 'Test', .05, lambda: False), 2)
        stop = False
        task = asyncio.create_task(self.meeting.join('teams', 'Test', 60, lambda: stop))
        await asyncio.sleep(.05)
        stop = True
        with self.assertRaisesRegex(InterruptedError, 'Stopped while joining'):
            await asyncio.wait_for(task, 2)

    async def test_guest_auth_compat_only_changes_empty_post(self):
        route = AsyncMock()
        request = Mock(method='POST', post_data_buffer=None)
        request.all_headers = AsyncMock(return_value={'cookie':'fixture-cookie', 'x-test':'preserved'})
        route.request = request
        await self.meeting._teams_visitor_auth(route)
        args = route.fallback.await_args.kwargs
        self.assertEqual(args['post_data'], b'{}')
        self.assertEqual(args['headers']['content-length'], '2')
        self.assertEqual(args['headers']['cookie'], 'fixture-cookie')
        self.assertIn('teams_proxy_auth_compat', self.events)
        for method, body in [('POST', b'{"provided":true}'), ('GET', None)]:
            route.reset_mock()
            request.method, request.post_data_buffer = method, body
            await self.meeting._teams_visitor_auth(route)
            route.fallback.assert_awaited_once_with()

    async def test_detached_frame_during_navigation_does_not_abort_join(self):
        stale = Mock()
        stale.is_detached.return_value = True
        stale.locator.return_value.count = AsyncMock(side_effect=RuntimeError('Frame was detached'))
        locator = stale.get_by_role.return_value.first
        locator.is_visible = AsyncMock(side_effect=RuntimeError('Frame was detached'))
        await self.meeting.page.set_content('<button>Leave</button><button>Participants</button>')
        original = self.meeting.page
        self.meeting.page = Mock(frames=[stale, original.main_frame], is_closed=Mock(return_value=False))
        try:
            self.assertTrue(await self.meeting.admitted())
            stale.is_detached.return_value = False
            with self.assertRaisesRegex(RuntimeError, 'Frame was detached'):
                await self.meeting.admitted()
        finally:
            self.meeting.page = original

    async def test_synthetic_track_pcm_capture_and_outgoing_mute(self):
        await self.meeting.page.evaluate('''async () => {
          const c = new AudioContext({sampleRate:24000}); await c.resume();
          const o=c.createOscillator(); o.frequency.value=440;
          const d=c.createMediaStreamDestination(); o.connect(d); o.start();
          window.fixture={c,o,d};
          await __meeting.attach(d.stream.getAudioTracks()[0]);
        }''')
        await asyncio.sleep(.6)
        self.assertGreaterEqual(len(self.packets), 2)
        data = b"".join(base64.b64decode(p["data"]) for p in self.packets)
        samples = struct.unpack("<" + "h"*(len(data)//2), data)
        self.assertGreater(math.sqrt(sum(x*x for x in samples)/len(samples)), 100)
        self.assertTrue(all(p["sample_rate"] == 24000 for p in self.packets))
        for a,b in zip(self.packets,self.packets[1:]):
            self.assertEqual(a["start_frame"]+a["frames"], b["start_frame"])
        muted = await self.meeting.page.evaluate('''() => {
          const pc=new RTCPeerConnection(); const track=fixture.d.stream.getAudioTracks()[0];
          pc.addTrack(track,fixture.d.stream); const muted=!track.enabled; pc.close(); return muted;
        }''')
        self.assertTrue(muted)

    async def test_deferred_capture_scans_receivers_and_is_ready_during_silence(self):
        await self.meeting.close()
        self.meeting = MeetingBrowser(self.runtime.browser, self.meeting.on_audio, self.meeting.event, defer_capture=True)
        await self.meeting.open()
        await self.meeting.page.set_content('<button>Leave</button><button>Participants</button>')
        await self.meeting.page.evaluate('''async () => {
          const c = new AudioContext({sampleRate:24000}); await c.resume();
          const d = c.createMediaStreamDestination();
          window.fixture={c,d};
          const track=d.stream.getAudioTracks()[0];
          // A track event was missed: receiver enumeration must recover it.
          __meeting.peers.push({getReceivers:() => [{track}],getSenders:() => []});
        }''')
        report = await self.meeting.capture_readiness()
        self.assertFalse(report['ready'])
        self.assertEqual(self.packets, [])
        report = await self.meeting.start_capture(timeout=1)
        self.assertTrue(report['ready'])
        self.assertEqual(report['frames'][0]['context_state'], 'running')
        self.assertIn('rx_ready', self.events)
        await asyncio.sleep(.2)
        self.assertGreater(len(self.packets), 0)
        self.assertTrue(all(not any(base64.b64decode(p['data'])) for p in self.packets))
        with self.assertRaisesRegex(RuntimeError, 'capture_not_permitted'):
            await self.meeting.start_capture()

    async def test_capture_readiness_failures_are_bounded_and_do_not_expose_js_text(self):
        await self.meeting.page.set_content('<button>Leave</button><button>Participants</button>')
        await self.meeting.page.evaluate("() => { window.__meeting = undefined; }")
        with self.assertRaisesRegex(RuntimeError, 'capture_hook_missing'):
            await self.meeting.start_capture(timeout=.2)
        sanitized = self.meeting._rx_readiness_report({'ready':True, 'hook_present':True,
            'capture_enabled':True, 'context_state':'running', 'live_attached_tracks':10**20,
            'attach_errors':['media_playout_failed','private-token https://teams.microsoft.com/meet/secret']})
        self.assertEqual(sanitized['live_attached_tracks'], 1000)
        self.assertEqual(sanitized['attach_errors'], ['media_playout_failed'])
        self.assertNotIn('private', json.dumps(sanitized))

    async def test_cancelled_sender_pacing_keeps_audio_tail_and_close_fails_closed(self):
        page = self.meeting.page
        await page.evaluate("""() => {
          window.fixtureNativePC = window.RTCPeerConnection;
          window.fixtureSessionClose = [];
          window.RTCPeerConnection = class extends EventTarget {
            constructor() { super(); this.iceGatheringState = 'complete'; }
            getConfiguration() { return {iceServers:[]}; }
            addTrack() {}
            createDataChannel() {
              return {readyState:'open', send:value => fixtureSessionClose.push(JSON.parse(value).type)};
            }
            async createOffer() { return {type:'offer', sdp:''}; }
            async setLocalDescription(value) { this.localDescription = value; }
          };
        }""")
        peer = LivePeer(self.runtime.browser, None)
        peer.page = page
        pushing = None
        try:
            # Execute the production scheduler on a real local AudioContext;
            # the peer/datachannel are in-page only and cannot open a network.
            await page.evaluate(PEER_JS)
            await page.evaluate("() => __live.ctx.suspend()")
            pushing = asyncio.create_task(peer.push(b'\x01\x00' * 52800))
            await page.wait_for_function("__live.sentFrames === 52800", timeout=1000)
            pushing.cancel()
            await asyncio.gather(pushing, return_exceptions=True)
            self.assertGreater(await page.evaluate("__live.next - __live.ctx.currentTime"), 2)
            with self.assertRaises(PlaywrightTimeoutError):
                await peer.request_session_close(timeout=.02)
            self.assertEqual(await page.evaluate("fixtureSessionClose"), [])
            await page.evaluate("() => __live.ctx.resume()")
            await page.wait_for_function("__live.ctx.currentTime >= __live.next", timeout=3000)
            self.assertTrue(await peer.request_session_close(timeout=.1))
            self.assertEqual(await page.evaluate("fixtureSessionClose"), ['session.close'])
        finally:
            if pushing and not pushing.done():
                pushing.cancel()
                await asyncio.gather(pushing, return_exceptions=True)
            await page.evaluate("""async () => {
              window.RTCPeerConnection = fixtureNativePC;
              if (window.__live?.ctx) await __live.ctx.close();
            }""")


    async def test_ice_diagnostics_exclude_addresses_and_credentials(self):
        await self.meeting.page.evaluate(r'''() => {
          const local = 'a=candidate:1 1 udp 100 192.0.2.10 4567 typ host\r\n' +
            'a=ice-pwd:private-ice-password\r\n';
          const remote = 'a=candidate:2 1 tcp 100 198.51.100.1 443 typ relay\r\n';
          window.__live = {pc: {
            connectionState:'failed', iceConnectionState:'disconnected',
            iceGatheringState:'complete', signalingState:'stable',
            localDescription:{sdp:local}, remoteDescription:{sdp:remote},
            getStats:async () => new Map([['private-id', {type:'candidate-pair',
              state:'failed', nominated:false, bytesSent:0, bytesReceived:0,
              address:'192.0.2.10', localCandidateId:'private-id'}]])
          }, dc:{readyState:'connecting'}, sentFrames:0};
        }''')
        peer = LivePeer(self.runtime.browser, None)
        peer.page = self.meeting.page
        result = await peer.stats()
        self.assertEqual(result['local_candidates'], {'count':1, 'udp':1, 'tcp':0, 'relay':0})
        self.assertEqual(result['remote_candidates'], {'count':1, 'udp':0, 'tcp':1, 'relay':1})
        self.assertEqual(result['candidate_pairs'][0]['state'], 'failed')
        serialized = json.dumps(result)
        for private in ['192.0.2.10', '198.51.100.1', 'private-id', 'private-ice-password', 'sdp']:
            self.assertNotIn(private, serialized)

    async def test_ice_error_survives_close_without_relay_secrets(self):
        await self.meeting.page.evaluate(r'''() => {
          const pc=new RTCPeerConnection({iceServers:[{
            urls:'turns:relay.example.test:443?transport=tcp',
            username:'private-username',credential:'private-password'}]});
          const e=new Event('icecandidateerror');
          Object.assign(e,{errorCode:701,url:'turns:203.0.113.9:443?transport=tcp',
            errorText:'private diagnostic text'});
          pc.dispatchEvent(e); pc.close();
        }''')
        result = await self.meeting.stats()
        diagnostic = result[0]['connections'][0]['ice_diagnostics']
        self.assertEqual(diagnostic['errors'], [{'code':701,'scheme':'turns','port':443,'transport':'tcp'}])
        self.assertEqual(diagnostic['servers'], [{'scheme':'turns','port':443,'transport':'tcp'}])
        for private in ['private-', 'relay.example.test', '203.0.113.9']:
            self.assertNotIn(private, json.dumps(result))
