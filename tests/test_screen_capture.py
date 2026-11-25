"""Screen capture, dHash change detection, and /dev/shm ring cache seam tests."""
import hashlib
import os
import stat
import unittest
from pathlib import Path

from teams_transcription.screen_capture import (
    ScreenFrameCache,
    ScreenShareCapturer,
    compute_dhash,
    hamming_distance,
)


class _FakeMeeting:
    def __init__(self, page):
        self.page = page


class ScreenCaptureUnitTests(unittest.TestCase):
    def test_powerpoint_live_url_allowlist_is_strict_and_rejects_spoofing(self):
        from teams_transcription.screen_capture import _is_powerpoint_live_url
        for valid in (
            'https://powerpoint.officeapps.live.com/p/PowerPointFrame.aspx?WOPISrc=abc',
            'https://apc-powerpoint.officeapps.live.com/p/PowerPointFrame.aspx',
            'https://euc-ppt.officeapps-df.live.com/p/frame',
            'https://contoso.sharepoint.com/sites/eng/_layouts/15/WopiFrame.aspx?file=deck.pptx',
            'https://contoso.sharepoint.com/_layouts/15/PowerPointFrame.aspx',
            'https://www.office.com/launch/powerpoint',
        ):
            with self.subTest(valid=valid):
                self.assertTrue(_is_powerpoint_live_url(valid))
        for invalid in (
            '',
            'about:blank',
            'http://powerpoint.officeapps.live.com/p/PowerPointFrame.aspx',
            'https://evil.example/powerpoint/PowerPointFrame.aspx',
            'https://evil.example/?src=https://powerpoint.officeapps.live.com/',
            'https://powerpoint.officeapps.live.com.evil.example/p/PowerPointFrame.aspx',
            'https://word-view.officeapps.live.com/wv/wordviewerframe.aspx',
            'https://contoso.sharepoint.com/sites/eng/chat?q=powerpoint',
        ):
            with self.subTest(invalid=invalid):
                self.assertFalse(_is_powerpoint_live_url(invalid))

    def test_dhash_distinguishes_slides_and_tolerates_tiny_noise(self):
        # Gradient left-to-right (all bits 1) vs right-to-left (all bits 0)
        slide_a = [x * 20 for _ in range(8) for x in range(9)]
        slide_b = [(8 - x) * 20 for _ in range(8) for x in range(9)]
        slide_a_noisy = [min(255, v + (1 if idx % 7 == 0 else 0)) for idx, v in enumerate(slide_a)]

        ha = compute_dhash(slide_a)
        hb = compute_dhash(slide_b)
        ha_noisy = compute_dhash(slide_a_noisy)

        self.assertEqual(len(ha), 16)
        self.assertEqual(hamming_distance(ha, ha_noisy), 0)
        self.assertGreaterEqual(hamming_distance(ha, hb), 32)

    def test_dhash_rejects_invalid_inputs(self):
        with self.assertRaises(ValueError):
            compute_dhash([0] * 71)
        with self.assertRaises(ValueError):
            hamming_distance('not-hex', '0' * 16)

    def test_shm_frame_cache_bounds_evicts_lru_and_cleans_completely(self):
        cache = ScreenFrameCache(max_frames=2, max_total_bytes=1024 * 1024)
        cache_dir = Path(cache.directory)
        try:
            self.assertTrue(str(cache_dir).startswith('/dev/shm/'))
            self.assertEqual(stat.S_IMODE(cache_dir.stat().st_mode), 0o700)

            entries = []
            for seq in (1, 2, 3):
                raw = f'PNG-FRAME-{seq}'.encode()
                sha = hashlib.sha256(raw).hexdigest()
                entry = cache.put(
                    raw, capture_seq=seq, frame_sha256=sha, dhash=f'{seq:016x}',
                    width=1280, height=720, capture_method='webrtc_video_frame',
                    captured_at='2026-11-24T13:50:00+00:00', mime='image/jpeg',
                )
                entries.append(entry)
                self.assertEqual(stat.S_IMODE(Path(entry.path).stat().st_mode), 0o600)

            # Frame 1 evicted, frames 2 & 3 retained
            self.assertIsNone(cache.get(entries[0].frame_sha256))
            self.assertFalse(Path(entries[0].path).exists())
            self.assertIsNotNone(cache.get(entries[1].frame_sha256))
            self.assertIsNotNone(cache.get(entries[2].frame_sha256))
            self.assertEqual(cache.latest().capture_seq, 3)
            self.assertEqual(cache.status()['cached_frames'], 2)
        finally:
            cleaned = cache.close()
        self.assertTrue(cleaned)
        self.assertFalse(cache_dir.exists())


class ScreenCaptureBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_captures_webrtc_video_share_detects_slide_change_and_ignores_webcam(self):
        from teams_transcription.browser import Browser
        exe = os.environ.get('ASSISTANT_CHROMIUM') or os.environ.get('MEETING_CHROMIUM')
        async with Browser(executable=exe) as runtime:
            assert runtime.browser is not None
            ctx = await runtime.browser.new_context(viewport={'width': 1280, 'height': 720})
            # 1) Page with ONLY a participant webcam tile -> must NOT capture and must not invoke locator RPCs
            page1 = await ctx.new_page()
            await page1.set_content("""
            <html><body>
              <div data-tid="participant-video-tile">
                <canvas id="cam" width="640" height="360"></canvas>
                <video id="camVideo" autoplay muted playsinline></video>
              </div>
              <script>
                (() => {
                  const c = document.getElementById('cam'), g = c.getContext('2d');
                  const paint = () => {
                    g.fillStyle = '#334155'; g.fillRect(0, 0, 640, 360);
                    g.fillStyle = '#f8fafc'; g.fillRect(200, 80, 240, 200);
                  };
                  paint();
                  const v = document.getElementById('camVideo');
                  v.srcObject = c.captureStream(10);
                  setInterval(paint, 50);
                  v.play().catch(() => {});
                })();
              </script>
            </body></html>""")
            await page1.wait_for_function("document.getElementById('camVideo').readyState >= 2")
            cap1 = ScreenShareCapturer(_FakeMeeting(page1))
            orig_locator = page1.locator
            locator_calls = []
            def _tracked_locator(*args, **kwargs):
                locator_calls.append(args)
                return orig_locator(*args, **kwargs)
            page1.locator = _tracked_locator
            self.assertIsNone(await cap1.capture())
            self.assertEqual(locator_calls, [])
            probe1 = cap1.probe_status()
            self.assertIsNotNone(probe1)
            self.assertEqual(probe1['videos_total'], 1)
            self.assertEqual(probe1['videos_ready'], 1)
            self.assertEqual(probe1['videos_excluded_camera'], 1)
            self.assertEqual(probe1['match_reason'], 'camera_excluded')
            await page1.close()

            # 2) Page with Teams sharing stage <video> -> captures webrtc_video_frame
            page2 = await ctx.new_page()
            await page2.set_content("""
            <html><body>
              <div data-tid="screen-share-stage">
                <canvas id="src" width="960" height="540"></canvas>
                <video id="shareVideo" autoplay muted playsinline style="width:960px;height:540px"></video>
              </div>
              <div role="log" aria-label="Chat">Secret chat text must not be captured</div>
              <script>
                (() => {
                  const c = document.getElementById('src'), g = c.getContext('2d');
                  window.slideMode = 1;
                  window.drawSlide = (mode) => {
                    window.slideMode = mode;
                    if (mode === 1) {
                      const grad = g.createLinearGradient(0, 0, 960, 0);
                      grad.addColorStop(0, '#000000'); grad.addColorStop(1, '#ffffff');
                      g.fillStyle = grad; g.fillRect(0, 0, 960, 540);
                      g.fillStyle = '#1d4ed8'; g.fillRect(40, 40, 880, 90);
                    } else {
                      const grad = g.createLinearGradient(960, 0, 0, 0);
                      grad.addColorStop(0, '#000000'); grad.addColorStop(1, '#ffffff');
                      g.fillStyle = grad; g.fillRect(0, 0, 960, 540);
                      g.fillStyle = '#b91c1c'; g.fillRect(40, 380, 880, 110);
                    }
                  };
                  window.drawSlide(1);
                  const v = document.getElementById('shareVideo');
                  v.srcObject = c.captureStream(15);
                  setInterval(() => window.drawSlide(window.slideMode), 50);
                  v.play().catch(() => {});
                })();
              </script>
            </body></html>""")
            await page2.wait_for_function("document.getElementById('shareVideo').readyState >= 2")
            capturer = ScreenShareCapturer(_FakeMeeting(page2))
            f1 = await capturer.capture()
            self.assertIsNotNone(f1)
            self.assertEqual(f1.capture_method, 'webrtc_video_frame')
            self.assertEqual((f1.width, f1.height), (960, 540))
            self.assertGreater(len(f1.image_bytes), 500)

            # Flip slide and verify dHash shifts significantly
            await page2.evaluate("window.drawSlide(2)")
            await page2.wait_for_timeout(150)
            f2 = await capturer.capture()
            self.assertIsNotNone(f2)
            self.assertNotEqual(f1.frame_sha256, f2.frame_sha256)
            self.assertGreaterEqual(hamming_distance(f1.dhash, f2.dhash), 16)
            await page2.close()

            # 3) Fallback to Teams stage DOM/canvas (e.g. PowerPoint Live / Whiteboard)
            page3 = await ctx.new_page()
            await page3.set_content("""
            <html><body>
              <div data-tid="screen-share-stage" style="width:800px;height:450px;background:#ffffff;padding:24px;">
                <h1 style="color:#0f172a;">PowerPoint Live Architecture</h1>
                <div style="width:400px;height:180px;background:#1e3a8a;color:white;padding:16px;">Module A -> Module B</div>
              </div>
              <div role="log" aria-label="Chat">Private chat sidebar</div>
            </body></html>""")
            f3 = await ScreenShareCapturer(_FakeMeeting(page3)).capture()
            self.assertIsNotNone(f3)
            self.assertEqual(f3.capture_method, 'teams_stage_dom')
            self.assertGreater(len(f3.image_bytes), 500)
            await page3.close()

            # 4) Generic calling-stage <video> WITHOUT explicit share evidence must NOT be captured
            page4 = await ctx.new_page()
            await page4.set_content("""
            <html><body>
              <div data-tid="calling-stage">
                <canvas id="stageCanvas" width="640" height="360"></canvas>
                <video id="stageVideo" autoplay muted playsinline style="width:400px;height:225px"></video>
              </div>
              <script>
                (() => {
                  const c = document.getElementById('stageCanvas'), g = c.getContext('2d');
                  const paint = () => {
                    g.fillStyle = '#0f172a'; g.fillRect(0, 0, 640, 360);
                    g.fillStyle = '#38bdf8'; g.fillRect(40, 40, 560, 120);
                  };
                  paint();
                  const v = document.getElementById('stageVideo');
                  v.srcObject = c.captureStream(10);
                  setInterval(paint, 50);
                  v.play().catch(() => {});
                })();
              </script>
            </body></html>""")
            await page4.wait_for_function("document.getElementById('stageVideo').readyState >= 2")
            cap4 = ScreenShareCapturer(_FakeMeeting(page4))
            self.assertIsNone(await cap4.capture())
            probe4 = cap4.probe_status()
            self.assertEqual(probe4['videos_ready'], 1)
            self.assertEqual(probe4['videos_unmatched'], 1)
            self.assertEqual(probe4['match_reason'], 'unmatched_video')
            await page4.close()

            # 4b) Same calling-stage structure WITH explicit share binding IS captured
            page4b = await ctx.new_page()
            await page4b.set_content("""
            <html><body>
              <div data-tid="calling-stage">
                <div data-stream-type="ScreenSharing">
                  <canvas id="stageShareCanvas" width="1280" height="720" style="display:none"></canvas>
                  <video id="stageShareVideo" autoplay muted playsinline style="width:960px;height:540px"></video>
                </div>
              </div>
              <script>
                (() => {
                  const c = document.getElementById('stageShareCanvas'), g = c.getContext('2d');
                  const paint = () => {
                    g.fillStyle = '#0f172a'; g.fillRect(0, 0, 1280, 720);
                    g.fillStyle = '#38bdf8'; g.fillRect(80, 80, 1120, 180);
                    g.fillStyle = '#f8fafc'; g.fillRect(80, 300, 720, 240);
                  };
                  paint();
                  const v = document.getElementById('stageShareVideo');
                  v.srcObject = c.captureStream(10);
                  setInterval(paint, 50);
                  v.play().catch(() => {});
                })();
              </script>
            </body></html>""")
            await page4b.wait_for_function("document.getElementById('stageShareVideo').readyState >= 2")
            cap4b = ScreenShareCapturer(_FakeMeeting(page4b))
            f4b = await cap4b.capture()
            self.assertIsNotNone(f4b)
            self.assertEqual(f4b.capture_method, 'webrtc_video_frame')
            self.assertEqual((f4b.width, f4b.height), (1280, 720))
            self.assertEqual(cap4b.probe_status()['match_reason'], 'explicit_share')
            await page4b.close()

            # 5) Open Shadow DOM <video> inside a Teams share container
            page5 = await ctx.new_page()
            await page5.set_content("""
            <html><body>
              <div data-stream-type="ScreenSharing" id="host"></div>
              <canvas id="shadowCanvas" width="960" height="540" style="display:none"></canvas>
              <script>
                (() => {
                  const host = document.getElementById('host');
                  const sr = host.attachShadow({mode: 'open'});
                  const v = document.createElement('video');
                  v.id = 'shadowVideo';
                  v.autoplay = true; v.muted = true; v.playsInline = true;
                  v.style.width = '640px'; v.style.height = '360px';
                  sr.appendChild(v);
                  const c = document.getElementById('shadowCanvas'), g = c.getContext('2d');
                  const paint = () => {
                    g.fillStyle = '#1e293b'; g.fillRect(0, 0, 960, 540);
                    g.fillStyle = '#22c55e'; g.fillRect(100, 100, 760, 280);
                  };
                  paint();
                  v.srcObject = c.captureStream(10);
                  setInterval(paint, 50);
                  v.play().catch(() => {});
                  window.__shadowVideo = v;
                })();
              </script>
            </body></html>""")
            await page5.wait_for_function("window.__shadowVideo && window.__shadowVideo.readyState >= 2")
            cap5 = ScreenShareCapturer(_FakeMeeting(page5))
            f5 = await cap5.capture()
            self.assertIsNotNone(f5)
            self.assertEqual(f5.capture_method, 'webrtc_video_frame')
            self.assertEqual(cap5.probe_status()['match_reason'], 'explicit_share')
            await page5.close()

            # 6) Large unlabelled 1280x720 <video> in a plain div (e.g. spotlight webcam) must NOT be captured
            page6 = await ctx.new_page()
            await page6.set_content("""
            <html><body style="margin:0">
              <div>
                <canvas id="domCanvas" width="1280" height="720" style="display:none"></canvas>
                <video id="domVideo" autoplay muted playsinline style="width:960px;height:540px"></video>
              </div>
              <script>
                (() => {
                  const c = document.getElementById('domCanvas'), g = c.getContext('2d');
                  const paint = () => {
                    g.fillStyle = '#ffffff'; g.fillRect(0, 0, 1280, 720);
                    g.fillStyle = '#1d4ed8'; g.fillRect(60, 60, 1160, 200);
                  };
                  paint();
                  const v = document.getElementById('domVideo');
                  v.srcObject = c.captureStream(10);
                  setInterval(paint, 50);
                  v.play().catch(() => {});
                })();
              </script>
            </body></html>""")
            await page6.wait_for_function("document.getElementById('domVideo').readyState >= 2")
            cap6 = ScreenShareCapturer(_FakeMeeting(page6))
            self.assertIsNone(await cap6.capture())
            probe6 = cap6.probe_status()
            self.assertEqual(probe6['videos_ready'], 1)
            self.assertEqual(probe6['videos_unmatched'], 1)
            self.assertEqual(probe6['max_video_width'], 1280)
            self.assertEqual(probe6['max_video_height'], 720)
            self.assertEqual(probe6['match_reason'], 'unmatched_video')
            await page6.close()

            # 7) PowerPoint Live embedded iframe (explicit title/aria-label, ZERO <video>) IS captured,
            #    while an iframe inside role="log" chat sidebar is ignored
            page7 = await ctx.new_page()
            await page7.set_content("""
            <html><body>
              <div role="log" aria-label="Chat">
                <iframe title="PowerPoint chat preview" style="width:400px;height:250px;border:0"
                  srcdoc="<html><body style='background:#ef4444'>Chat embed must not capture</body></html>"></iframe>
              </div>
              <iframe id="pptFrame" title="PowerPoint Live presentation" style="width:800px;height:450px;border:0"
                srcdoc="<html><body style='margin:0;background:#ffffff;padding:24px;font-family:sans-serif'><h1 style='color:#0f172a'>Q4 Roadmap</h1><div style='width:500px;height:200px;background:#1d4ed8;color:#fff;padding:16px'>Milestone 1 -> Milestone 2</div></body></html>"></iframe>
            </body></html>""")
            cap7 = ScreenShareCapturer(_FakeMeeting(page7))
            f7 = await cap7.capture()
            self.assertIsNotNone(f7)
            self.assertEqual(f7.capture_method, 'teams_stage_dom')
            self.assertGreater(len(f7.image_bytes), 500)
            probe7 = cap7.probe_status()
            self.assertGreaterEqual(probe7['iframes_total'], 2)
            self.assertEqual(probe7['match_reason'], 'stage_dom_candidate')
            await page7.close()

            # 8) PowerPoint Live child frame routed at https://apc-powerpoint.officeapps.live.com
            #    with NO data-tid/title on the iframe element -> matched via strict frame.url allowlist
            page8 = await ctx.new_page()
            async def ppt_route(route):
                url = route.request.url
                if url == 'https://teams.microsoft.com/mock-meeting':
                    await route.fulfill(status=200, content_type='text/html', body=(
                        '<html><body style="margin:0">'
                        '<iframe style="width:800px;height:450px;border:0" '
                        'src="https://apc-powerpoint.officeapps.live.com/p/PowerPointFrame.aspx"></iframe>'
                        '</body></html>'))
                elif url.startswith('https://apc-powerpoint.officeapps.live.com/'):
                    await route.fulfill(status=200, content_type='text/html', body=(
                        '<html><body style="margin:0;background:#f8fafc;padding:24px">'
                        '<h1 style="color:#0f172a">PowerPoint Live Slide 3</h1>'
                        '<div style="width:600px;height:220px;background:#0f766e"></div>'
                        '</body></html>'))
                else:
                    await route.abort()
            await page8.route('**/*', ppt_route)
            await page8.goto('https://teams.microsoft.com/mock-meeting', wait_until='load')
            cap8 = ScreenShareCapturer(_FakeMeeting(page8))
            f8 = await cap8.capture()
            self.assertIsNotNone(f8)
            self.assertEqual(f8.capture_method, 'teams_stage_dom')
            self.assertGreater(len(f8.image_bytes), 500)
            probe8 = cap8.probe_status()
            self.assertEqual(probe8['powerpoint_frames_seen'], 1)
            self.assertEqual(probe8['match_reason'], 'powerpoint_live_frame')
            await page8.close()

            await ctx.close()

    def test_screen_context_diagnostic_allowlists_probe_without_leaking_raw_strings(self):
        from teams_transcription.diagnostics import screen_context_diagnostic
        diag = screen_context_diagnostic({
            'mode': 'watch_and_cache',
            'started': True,
            'share_active': False,
            'cleanup_status': 'idle',
            'probe': {
                'frames_checked': 2,
                'videos_total': 3,
                'videos_ready': 2,
                'videos_excluded_camera': 1,
                'videos_unmatched': 1,
                'max_video_width': 1920,
                'max_video_height': 1080,
                'max_rect_width': 1280,
                'max_rect_height': 720,
                'match_reason': 'PRIVATE https://secret.invalid/dom',
                'raw_dom': 'MUST_NOT_APPEAR',
            },
        })
        self.assertEqual(diag['probe']['match_reason'], 'unknown')
        self.assertEqual(diag['probe']['max_video_width'], 1920)
        self.assertNotIn('raw_dom', diag['probe'])
        self.assertNotIn('PRIVATE', str(diag))
        self.assertNotIn('MUST_NOT_APPEAR', str(diag))


if __name__ == '__main__':
    unittest.main()
