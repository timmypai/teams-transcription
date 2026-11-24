"""Teams screen-share capture, perceptual dHash, and /dev/shm ring frame cache.

Captures only active incoming screen-share frames (WebRTC <video> first,
Teams stage DOM/canvas fallback for PowerPoint Live / Whiteboard). Never
enables local camera/video senders or writes images to persistent storage.
"""
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile


DEFAULT_MAX_FRAMES = 16
DEFAULT_MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_VIDEO_WIDTH = 1920
MAX_VIDEO_HEIGHT = 1080


def _verify_tmpfs_root(root='/dev/shm'):
    path = Path(root)
    try:
        mounts = Path('/proc/self/mountinfo').read_text().splitlines()
        memory_backed = False
        for mount in mounts:
            fields, separator, filesystem = mount.partition(' - ')
            parts = fields.split()
            if separator and len(parts) > 4 and parts[4] == str(path):
                memory_backed = filesystem.split()[0] == 'tmpfs'
        if not memory_backed or path.is_symlink():
            raise RuntimeError('screen_cache_requires_tmpfs')
        return path
    except OSError:
        raise RuntimeError('screen_cache_requires_tmpfs') from None


def compute_dhash(luma_9x8):
    """Compute a 64-bit difference hash (16 lowercase hex chars) from 9x8 luma."""
    if not isinstance(luma_9x8, (list, tuple)) or len(luma_9x8) != 72:
        raise ValueError('dhash_requires_72_luma_values')
    bits = 0
    for row in range(8):
        offset = row * 9
        for col in range(8):
            left = luma_9x8[offset + col]
            right = luma_9x8[offset + col + 1]
            if (isinstance(left, bool) or isinstance(right, bool)
                    or not isinstance(left, (int, float)) or not isinstance(right, (int, float))):
                raise ValueError('dhash_requires_numeric_luma')
            bits = (bits << 1) | (1 if left < right else 0)
    return f'{bits:016x}'


def hamming_distance(first, second):
    """Bitwise Hamming distance between two 16-hex-char 64-bit dHashes."""
    if (not isinstance(first, str) or not isinstance(second, str)
            or not re.fullmatch(r'[0-9a-f]{16}', first)
            or not re.fullmatch(r'[0-9a-f]{16}', second)):
        raise ValueError('invalid_dhash_hex')
    return (int(first, 16) ^ int(second, 16)).bit_count()


@dataclass(frozen=True)
class CapturedFrame:
    image_bytes: bytes
    mime: str
    width: int
    height: int
    dhash: str
    frame_sha256: str
    capture_method: str
    captured_at: str


@dataclass(frozen=True)
class CachedFrame:
    capture_seq: int
    frame_sha256: str
    dhash: str
    width: int
    height: int
    capture_method: str
    captured_at: str
    path: str
    size_bytes: int
    mime: str


class ScreenFrameCache:
    """Bounded in-RAM (/dev/shm tmpfs) ring buffer for raw screen-share frames."""

    def __init__(self, *, max_frames=DEFAULT_MAX_FRAMES, max_total_bytes=DEFAULT_MAX_TOTAL_BYTES, root='/dev/shm'):
        if type(max_frames) is not int or not 1 <= max_frames <= 64:
            raise ValueError('invalid_frame_cache_limit')
        if type(max_total_bytes) is not int or max_total_bytes < 65536:
            raise ValueError('invalid_frame_cache_bytes')
        verified_root = _verify_tmpfs_root(root)
        self.max_frames = max_frames
        self.max_total_bytes = max_total_bytes
        self._dir = Path(tempfile.mkdtemp(prefix='meeting-screen-', dir=verified_root))
        os.chmod(self._dir, 0o700)
        self._entries = OrderedDict()
        self._total_bytes = 0
        self._closed = False

    @property
    def directory(self):
        return str(self._dir)

    def put(self, image_bytes, *, capture_seq, frame_sha256, dhash, width, height,
            capture_method, captured_at, mime='image/jpeg'):
        if self._closed:
            raise RuntimeError('screen_frame_cache_closed')
        if (not isinstance(image_bytes, (bytes, bytearray)) or not 0 < len(image_bytes) <= MAX_FRAME_BYTES
                or not isinstance(frame_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', frame_sha256)):
            raise ValueError('invalid_cached_frame')
        if frame_sha256 in self._entries:
            self._entries.move_to_end(frame_sha256)
            return self._entries[frame_sha256]
        ext = '.png' if mime == 'image/png' else '.jpg'
        target = self._dir / f'f-{capture_seq:05d}-{frame_sha256[:12]}{ext}'
        fd, temp_path = tempfile.mkstemp(prefix='.frame-', dir=self._dir)
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(image_bytes)
            os.replace(temp_path, target)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        entry = CachedFrame(
            capture_seq=capture_seq, frame_sha256=frame_sha256, dhash=dhash,
            width=width, height=height, capture_method=capture_method,
            captured_at=captured_at, path=str(target), size_bytes=len(image_bytes), mime=mime,
        )
        self._entries[frame_sha256] = entry
        self._total_bytes += entry.size_bytes
        while len(self._entries) > self.max_frames or (
                len(self._entries) > 1 and self._total_bytes > self.max_total_bytes):
            _, oldest = self._entries.popitem(last=False)
            self._total_bytes = max(0, self._total_bytes - oldest.size_bytes)
            try:
                os.unlink(oldest.path)
            except FileNotFoundError:
                pass
        return entry

    def get(self, frame_sha256):
        if self._closed:
            return None
        entry = self._entries.get(frame_sha256)
        if entry is None:
            return None
        if not os.path.isfile(entry.path):
            self._entries.pop(frame_sha256, None)
            self._total_bytes = max(0, self._total_bytes - entry.size_bytes)
            return None
        return entry

    def get_by_seq(self, capture_seq):
        if self._closed:
            return None
        for entry in reversed(self._entries.values()):
            if entry.capture_seq == capture_seq and os.path.isfile(entry.path):
                return entry
        return None

    def latest(self):
        if self._closed or not self._entries:
            return None
        for entry in reversed(self._entries.values()):
            if os.path.isfile(entry.path):
                return entry
        return None

    def list_entries(self):
        if self._closed:
            return []
        return [entry for entry in self._entries.values() if os.path.isfile(entry.path)]

    def status(self):
        return {
            'cached_frames': len(self._entries),
            'cached_bytes': self._total_bytes,
            'max_frames': self.max_frames,
            'closed': self._closed,
        }

    def close(self):
        self._closed = True
        self._entries.clear()
        self._total_bytes = 0
        if self._dir.exists():
            shutil.rmtree(self._dir, ignore_errors=False)
        return not self._dir.exists()


CAPTURE_SHARE_VIDEO_JS = r"""() => {
  const hook = window.__meeting;
  if (hook && Array.isArray(hook.peers)) {
    for (const pc of hook.peers) {
      for (const sender of pc.getSenders?.() || []) {
        if (sender.track && sender.track.kind === 'video' && sender.track.enabled) {
          throw new Error('outgoing_video_sender_enabled');
        }
      }
    }
  }
  const shareSelectors = [
    '[data-tid*="screen-share" i]', '[data-tid*="screenshare" i]',
    '[data-tid*="sharing" i]', '[data-tid*="share-stage" i]',
    '[data-tid*="content-sharing" i]', '[aria-label*="screen share" i]',
    '[aria-label*="sharing" i]', '[aria-label*="分享畫面" i]', '[aria-label*="共享" i]'
  ];
  const cameraExclude = [
    '[data-tid*="participant-video" i]', '[data-tid*="video-tile" i]',
    '[data-tid*="camera" i]', '[data-tid*="self-video" i]',
    '[role="log"]', '[role="feed"]'
  ];
  const isInside = (el, selectors) => selectors.some(sel => {
    try { return Boolean(el.closest(sel)); } catch (_) { return false; }
  });
  const candidates = [];
  for (const video of document.querySelectorAll('video')) {
    if (video.readyState < 2 || !video.videoWidth || !video.videoHeight) continue;
    if (video.videoWidth < 320 || video.videoHeight < 180) continue;
    const rect = video.getBoundingClientRect();
    if (rect.width < 160 || rect.height < 90) continue;
    if (isInside(video, cameraExclude) && !isInside(video, shareSelectors)) continue;
    const explicitShare = isInside(video, shareSelectors);
    const label = ((video.getAttribute('aria-label') || '') + ' ' + (video.getAttribute('data-tid') || '')).toLowerCase();
    const labelShare = /screen|share|present|slide|content|分享|簡報|投影片/.test(label);
    const trackLabel = (() => {
      try {
        const tracks = video.srcObject?.getVideoTracks?.() || [];
        return tracks.map(t => (t.label || '') + ' ' + (t.contentHint || '')).join(' ').toLowerCase();
      } catch (_) { return ''; }
    })();
    const trackShare = /screen|share|detail|text|desktop|window|monitor/.test(trackLabel);
    if (!explicitShare && !labelShare && !trackShare) continue;
    candidates.push({
      video,
      priority: (explicitShare ? 4 : 0) + (labelShare ? 2 : 0) + (trackShare ? 1 : 0),
      area: video.videoWidth * video.videoHeight,
    });
  }
  if (!candidates.length) return null;
  candidates.sort((a, b) => (b.priority - a.priority) || (b.area - a.area));
  const best = candidates[0].video;
  const srcW = best.videoWidth, srcH = best.videoHeight;
  const scale = Math.min(1, 1920 / srcW, 1080 / srcH);
  const outW = Math.max(1, Math.round(srcW * scale));
  const outH = Math.max(1, Math.round(srcH * scale));

  // Compute 9x8 luma grid for perceptual dHash
  const small = document.createElement('canvas');
  small.width = 9; small.height = 8;
  const sg = small.getContext('2d', {willReadFrequently: true});
  sg.drawImage(best, 0, 0, 9, 8);
  const rgba = sg.getImageData(0, 0, 9, 8).data;
  const luma = [];
  let minL = 255, maxL = 0;
  for (let i = 0; i < rgba.length; i += 4) {
    const y = Math.round(0.299 * rgba[i] + 0.587 * rgba[i + 1] + 0.114 * rgba[i + 2]);
    luma.push(y);
    if (y < minL) minL = y;
    if (y > maxL) maxL = y;
  }
  if (maxL - minL < 3 && maxL < 8) return {blank: true};

  const full = document.createElement('canvas');
  full.width = outW; full.height = outH;
  const fg = full.getContext('2d');
  fg.drawImage(best, 0, 0, outW, outH);
  const dataUrl = full.toDataURL('image/jpeg', 0.85);
  const comma = dataUrl.indexOf(',');
  if (comma < 0) return null;
  return {
    blank: false,
    mime: 'image/jpeg',
    data: dataUrl.slice(comma + 1),
    width: outW,
    height: outH,
    luma,
  };
}"""

COMPUTE_IMAGE_LUMA_JS = r"""async ({mime, data}) => {
  const img = new Image();
  await new Promise((resolve, reject) => {
    img.onload = resolve;
    img.onerror = () => reject(new Error('stage_image_decode_failed'));
    img.src = `data:${mime};base64,${data}`;
  });
  const c = document.createElement('canvas');
  c.width = 9; c.height = 8;
  const g = c.getContext('2d', {willReadFrequently: true});
  g.drawImage(img, 0, 0, 9, 8);
  const rgba = g.getImageData(0, 0, 9, 8).data;
  const luma = [];
  for (let i = 0; i < rgba.length; i += 4) {
    luma.push(Math.round(0.299 * rgba[i] + 0.587 * rgba[i + 1] + 0.114 * rgba[i + 2]));
  }
  return {width: img.naturalWidth, height: img.naturalHeight, luma};
}"""

_STAGE_SELECTORS = (
    '[data-tid*="screen-share" i]',
    '[data-tid*="screenshare" i]',
    '[data-tid*="powerpoint" i]',
    '[data-tid*="whiteboard" i]',
    '[data-tid*="share-stage" i]',
    '[data-tid*="content-sharing" i]',
)


class ScreenShareCapturer:
    """Captures active Teams screen-share frames (WebRTC <video> or stage DOM)."""

    def __init__(self, meeting_or_page, *, clock=None):
        self._source = meeting_or_page
        self._clock = clock

    def _page(self):
        page = getattr(self._source, 'page', self._source)
        if page is None or (callable(getattr(page, 'is_closed', None)) and page.is_closed()):
            return None
        return page

    def _now_iso(self):
        return datetime.now(timezone.utc).isoformat()

    async def capture(self):
        page = self._page()
        if page is None:
            return None
        frames = list(getattr(page, 'frames', None) or [page])
        # 1) Primary path: WebRTC share <video> native resolution frame
        for frame in frames:
            if callable(getattr(frame, 'is_detached', None)) and frame.is_detached():
                continue
            try:
                result = await frame.evaluate(CAPTURE_SHARE_VIDEO_JS)
            except Exception as exc:
                if 'outgoing_video_sender_enabled' in str(exc):
                    raise RuntimeError('outgoing_video_sender_enabled') from None
                continue
            if not isinstance(result, dict) or result.get('blank') is True:
                continue
            raw = base64.b64decode(result['data'], validate=True)
            if not 0 < len(raw) <= MAX_FRAME_BYTES:
                continue
            dhash = compute_dhash(result['luma'])
            return CapturedFrame(
                image_bytes=raw,
                mime=result.get('mime', 'image/jpeg'),
                width=int(result['width']),
                height=int(result['height']),
                dhash=dhash,
                frame_sha256=hashlib.sha256(raw).hexdigest(),
                capture_method='webrtc_video_frame',
                captured_at=self._now_iso(),
            )

        # 2) Fallback path: PowerPoint Live / Whiteboard / stage DOM container
        for frame in frames:
            if callable(getattr(frame, 'is_detached', None)) and frame.is_detached():
                continue
            for selector in _STAGE_SELECTORS:
                try:
                    locator = frame.locator(selector).first
                    if await locator.count() == 0 or not await locator.is_visible():
                        continue
                    box = await locator.bounding_box()
                    if not box or box.get('width', 0) < 160 or box.get('height', 0) < 90:
                        continue
                    raw = await locator.screenshot(type='jpeg', quality=85, timeout=3000)
                except Exception:
                    continue
                if not raw or len(raw) > MAX_FRAME_BYTES:
                    continue
                encoded = base64.b64encode(raw).decode('ascii')
                meta = await page.evaluate(COMPUTE_IMAGE_LUMA_JS, {'mime': 'image/jpeg', 'data': encoded})
                dhash = compute_dhash(meta['luma'])
                return CapturedFrame(
                    image_bytes=raw,
                    mime='image/jpeg',
                    width=int(meta['width']),
                    height=int(meta['height']),
                    dhash=dhash,
                    frame_sha256=hashlib.sha256(raw).hexdigest(),
                    capture_method='teams_stage_dom',
                    captured_at=self._now_iso(),
                )
        return None
