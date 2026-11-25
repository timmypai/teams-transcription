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
            # 1) Page with ONLY a participant webcam tile -> must NOT capture
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
            self.assertIsNone(await ScreenShareCapturer(_FakeMeeting(page1)).capture())
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
            await ctx.close()


if __name__ == '__main__':
    unittest.main()
