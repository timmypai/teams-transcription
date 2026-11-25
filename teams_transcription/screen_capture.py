"""Teams screen-share capture, perceptual dHash, and /dev/shm ring frame cache.

Captures only active incoming screen-share frames (WebRTC <video> first,
Teams stage DOM/canvas fallback for PowerPoint Live / Whiteboard). Never
enables local camera/video senders or writes images to persistent storage.
"""
import asyncio
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
from urllib.parse import urlparse


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


CAPTURE_SHARE_VIDEO_JS = r"""(stageDomSelectors = []) => {
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
    '[data-tid*="screen_share" i]', '[data-tid*="sharing" i]',
    '[data-tid*="share-stage" i]', '[data-tid*="content-sharing" i]',
    '[data-tid*="shared-content" i]', '[data-tid*="presentation" i]',
    '[data-tid*="content-stage" i]', '[data-tid*="calling-screen" i]',
    '[data-tid*="stage-screen" i]',
    '[data-stream-type*="screen" i]', '[data-stream-type*="sharing" i]',
    '[data-stream-type*="content" i]',
    '[data-cid*="screen" i]', '[data-cid*="sharing" i]',
    '[aria-label*="screen share" i]', '[aria-label*="shared screen" i]',
    '[aria-label*="sharing" i]', '[aria-label*="presenting" i]',
    '[aria-label*="presentation" i]',
    '[aria-label*="分享畫面" i]', '[aria-label*="共用畫面" i]',
    '[aria-label*="共享" i]', '[aria-label*="簡報" i]'
  ];
  const cameraExclude = [
    '[data-tid*="participant-video" i]', '[data-tid*="video-tile" i]',
    '[data-tid*="camera" i]', '[data-tid*="self-video" i]',
    '[role="log"]', '[role="feed"]'
  ];
  const matchesAny = (el, selectors) => selectors.some(sel => {
    try { return Boolean(el && typeof el.matches === 'function' && el.matches(sel)); } catch (_) { return false; }
  });
  const closestAcrossShadow = (el, selectors) => {
    let cur = el, depth = 0;
    while (cur && depth < 24) {
      if (matchesAny(cur, selectors)) return true;
      if (cur.parentElement) {
        cur = cur.parentElement;
      } else {
        const root = typeof cur.getRootNode === 'function' ? cur.getRootNode() : null;
        cur = root && root.host ? root.host : null;
      }
      depth++;
    }
    return false;
  };
  const collectDomMedia = () => {
    const videos = [];
    const seenVideos = new Set();
    let iframesTotal = 0;
    let canvasesLarge = 0;
    const stack = [[document, 0]];
    let rootsVisited = 0;
    let nodesVisited = 0;
    while (stack.length && rootsVisited < 256 && nodesVisited < 8000) {
      const [root, depth] = stack.pop();
      if (!root || depth > 12) continue;
      rootsVisited++;
      for (const v of root.querySelectorAll('video')) {
        if (!seenVideos.has(v)) {
          seenVideos.add(v);
          videos.push(v);
        }
      }
      iframesTotal += root.querySelectorAll('iframe').length;
      for (const c of root.querySelectorAll('canvas')) {
        try {
          if (closestAcrossShadow(c, ['[role="log"]', '[role="feed"]'])) continue;
          const cr = c.getBoundingClientRect();
          if (cr.width >= 320 && cr.height >= 180) canvasesLarge++;
        } catch (_) {}
      }
      if (depth < 12) {
        const all = root.querySelectorAll('*');
        for (let i = 0; i < all.length && nodesVisited < 8000; i++) {
          nodesVisited++;
          const sr = all[i].shadowRoot;
          if (sr) stack.push([sr, depth + 1]);
        }
      }
    }
    return {videos, iframesTotal, canvasesLarge};
  };
  const findStageDomCandidate = () => {
    if (!Array.isArray(stageDomSelectors)) return {selectorIndex: -1, matchIndex: 0};
    for (let i = 0; i < stageDomSelectors.length; i++) {
      try {
        const list = document.querySelectorAll(stageDomSelectors[i]);
        for (let j = 0; j < list.length; j++) {
          const el = list[j];
          if (!el || el.tagName === 'IFRAME') continue;
          if (closestAcrossShadow(el, ['[role="log"]', '[role="feed"]'])) continue;
          const style = window.getComputedStyle(el);
          if (!style || style.display === 'none' || style.visibility === 'hidden') continue;
          const rect = el.getBoundingClientRect();
          if (rect.width >= 160 && rect.height >= 90) return {selectorIndex: i, matchIndex: j};
        }
      } catch (_) {}
    }
    return {selectorIndex: -1, matchIndex: 0};
  };

  const domMedia = collectDomMedia();
  const probe = {
    videos_total: 0,
    videos_ready: 0,
    videos_excluded_camera: 0,
    videos_unmatched: 0,
    iframes_total: domMedia.iframesTotal,
    canvases_large: domMedia.canvasesLarge,
    max_video_width: 0,
    max_video_height: 0,
    max_rect_width: 0,
    max_rect_height: 0,
    match_reason: 'no_video',
  };
  const candidates = [];
  let sawBelowSize = false;
  for (const video of domMedia.videos) {
    probe.videos_total++;
    if (video.readyState < 2 || !video.videoWidth || !video.videoHeight) continue;
    probe.videos_ready++;
    if (video.videoWidth > probe.max_video_width) probe.max_video_width = Math.round(video.videoWidth);
    if (video.videoHeight > probe.max_video_height) probe.max_video_height = Math.round(video.videoHeight);
    const rect = video.getBoundingClientRect();
    const rw = Math.max(0, Math.round(rect.width || 0));
    const rh = Math.max(0, Math.round(rect.height || 0));
    if (rw > probe.max_rect_width) probe.max_rect_width = rw;
    if (rh > probe.max_rect_height) probe.max_rect_height = rh;
    if (video.videoWidth < 320 || video.videoHeight < 180 || rect.width < 160 || rect.height < 90) {
      sawBelowSize = true;
      continue;
    }
    const explicitShare = closestAcrossShadow(video, shareSelectors);
    const inCameraExclude = closestAcrossShadow(video, cameraExclude);
    if (inCameraExclude && !explicitShare) {
      probe.videos_excluded_camera++;
      continue;
    }
    const label = [
      video.getAttribute('aria-label') || '',
      video.getAttribute('data-tid') || '',
      video.getAttribute('data-stream-type') || '',
      video.getAttribute('data-cid') || '',
      video.getAttribute('title') || '',
    ].join(' ').toLowerCase();
    const labelShare = /screen|share|present|slide|content|分享|共用|簡報|投影片/.test(label);
    const trackLabel = (() => {
      try {
        const tracks = video.srcObject?.getVideoTracks?.() || [];
        return tracks.map(t => (t.label || '') + ' ' + (t.contentHint || '')).join(' ').toLowerCase();
      } catch (_) { return ''; }
    })();
    const trackShare = /screen|share|detail|text|desktop|window|monitor/.test(trackLabel);
    if (!explicitShare && !labelShare && !trackShare) {
      probe.videos_unmatched++;
      continue;
    }
    const reason = explicitShare ? 'explicit_share'
      : labelShare ? 'label_share'
      : 'track_share';
    candidates.push({
      video,
      reason,
      priority: (explicitShare ? 4 : 0) + (labelShare ? 2 : 0) + (trackShare ? 1 : 0),
      area: video.videoWidth * video.videoHeight,
    });
  }
  if (!candidates.length) {
    const stageHit = findStageDomCandidate();
    probe.match_reason = stageHit.selectorIndex >= 0 ? 'stage_dom_candidate'
      : probe.videos_excluded_camera > 0 ? 'camera_excluded'
      : probe.videos_unmatched > 0 ? 'unmatched_video'
      : sawBelowSize ? 'below_size'
      : 'no_video';
    return {
      matched: false,
      blank: false,
      stage_selector_index: stageHit.selectorIndex,
      stage_match_index: stageHit.matchIndex,
      probe,
    };
  }
  candidates.sort((a, b) => (b.priority - a.priority) || (b.area - a.area));
  const best = candidates[0];
  const bestVideo = best.video;
  const srcW = bestVideo.videoWidth, srcH = bestVideo.videoHeight;
  const scale = Math.min(1, 1920 / srcW, 1080 / srcH);
  const outW = Math.max(1, Math.round(srcW * scale));
  const outH = Math.max(1, Math.round(srcH * scale));

  // Compute 9x8 luma grid for perceptual dHash
  const small = document.createElement('canvas');
  small.width = 9; small.height = 8;
  const sg = small.getContext('2d', {willReadFrequently: true});
  sg.drawImage(bestVideo, 0, 0, 9, 8);
  const rgba = sg.getImageData(0, 0, 9, 8).data;
  const luma = [];
  let minL = 255, maxL = 0;
  for (let i = 0; i < rgba.length; i += 4) {
    const y = Math.round(0.299 * rgba[i] + 0.587 * rgba[i + 1] + 0.114 * rgba[i + 2]);
    luma.push(y);
    if (y < minL) minL = y;
    if (y > maxL) maxL = y;
  }
  if (maxL - minL < 3 && maxL < 8) {
    const stageHit = findStageDomCandidate();
    probe.match_reason = stageHit.selectorIndex >= 0 ? 'stage_dom_candidate' : 'blank_video';
    return {
      matched: false,
      blank: true,
      stage_selector_index: stageHit.selectorIndex,
      stage_match_index: stageHit.matchIndex,
      probe,
    };
  }

  const full = document.createElement('canvas');
  full.width = outW; full.height = outH;
  const fg = full.getContext('2d');
  fg.drawImage(bestVideo, 0, 0, outW, outH);
  const dataUrl = full.toDataURL('image/jpeg', 0.85);
  const comma = dataUrl.indexOf(',');
  if (comma < 0) {
    probe.match_reason = 'evaluate_error';
    return {matched: false, blank: false, stage_selector_index: -1, probe};
  }
  probe.match_reason = best.reason;
  return {
    matched: true,
    blank: false,
    mime: 'image/jpeg',
    data: dataUrl.slice(comma + 1),
    width: outW,
    height: outH,
    luma,
    probe,
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
    '[data-tid*="screen_share" i]',
    '[data-tid*="powerpoint" i]',
    '[data-tid*="ppt" i]',
    '[data-tid*="whiteboard" i]',
    '[data-tid*="share-stage" i]',
    '[data-tid*="content-sharing" i]',
    '[data-tid*="shared-content" i]',
    '[data-tid*="calling-screen-sharing" i]',
    '[data-tid*="presentation" i]',
    '[data-tid*="live-share" i]',
    '[data-stream-type*="screen" i]',
    '[data-stream-type*="sharing" i]',
    '[data-cid*="screen-share" i]',
    '[data-cid*="sharing" i]',
    '[data-cid*="powerpoint" i]',
)

_REASON_PRIORITY = {
    'explicit_share': 9,
    'label_share': 8,
    'track_share': 7,
    'powerpoint_live_frame': 6,
    'stage_dom_candidate': 5,
    'blank_video': 4,
    'camera_excluded': 3,
    'unmatched_video': 2,
    'below_size': 1,
    'no_video': 0,
    'evaluate_error': 0,
    'page_unavailable': 0,
}


def _is_powerpoint_live_url(url):
    """Strict allowlist for Microsoft PowerPoint Live / Office Online iframe URLs."""
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme.lower() != 'https':
        return False
    host = (parsed.hostname or '').lower().rstrip('.')
    if not host:
        return False
    first_label = host.split('.', 1)[0]
    if host in {'powerpoint.officeapps.live.com', 'powerpoint.office.com'} or host.endswith('.powerpoint.office.com'):
        return True
    if host.endswith('.officeapps.live.com') or host.endswith('.officeapps-df.live.com'):
        if 'powerpoint' in first_label or 'ppt' in first_label:
            return True
        path = (parsed.path or '').lower()
        return any(tok in path for tok in ('powerpoint', '/ppt', 'pptview'))
    if (host.endswith('.sharepoint.com')
            or host.endswith('.sharepoint-df.com')
            or host == 'www.office.com'
            or host.endswith('.office.com')):
        path = (parsed.path or '').lower()
        return any(tok in path for tok in ('powerpoint', 'wopiframe', '/ppt', 'pptview'))
    return False


class ScreenShareCapturer:
    """Captures active Teams screen-share frames (WebRTC <video> or stage DOM)."""

    def __init__(self, meeting_or_page, *, clock=None, evaluate_timeout=2.0):
        self._source = meeting_or_page
        self._clock = clock
        self._evaluate_timeout = evaluate_timeout
        self._last_probe = None

    def probe_status(self):
        return dict(self._last_probe) if self._last_probe is not None else None

    def _page(self):
        page = getattr(self._source, 'page', self._source)
        if page is None or (callable(getattr(page, 'is_closed', None)) and page.is_closed()):
            return None
        return page

    def _now_iso(self):
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _merge_probe(agg, item):
        if not isinstance(item, dict):
            return
        for key in (
            'videos_total', 'videos_ready', 'videos_excluded_camera', 'videos_unmatched',
            'iframes_total', 'canvases_large',
        ):
            val = item.get(key)
            if type(val) is int and val >= 0:
                agg[key] += val
        for key in ('max_video_width', 'max_video_height', 'max_rect_width', 'max_rect_height'):
            val = item.get(key)
            if type(val) is int and val > agg[key]:
                agg[key] = val
        reason = item.get('match_reason')
        if isinstance(reason, str) and _REASON_PRIORITY.get(reason, -1) > _REASON_PRIORITY.get(agg['match_reason'], -1):
            agg['match_reason'] = reason

    async def capture(self):
        page = self._page()
        if page is None:
            self._last_probe = {
                'frames_checked': 0, 'videos_total': 0, 'videos_ready': 0,
                'videos_excluded_camera': 0, 'videos_unmatched': 0,
                'iframes_total': 0, 'canvases_large': 0, 'powerpoint_frames_seen': 0,
                'max_video_width': 0, 'max_video_height': 0,
                'max_rect_width': 0, 'max_rect_height': 0,
                'match_reason': 'page_unavailable',
            }
            return None
        main_frame = getattr(page, 'main_frame', page)
        frames = list(getattr(page, 'frames', None) or [page])
        agg = {
            'frames_checked': 0, 'videos_total': 0, 'videos_ready': 0,
            'videos_excluded_camera': 0, 'videos_unmatched': 0,
            'iframes_total': 0, 'canvases_large': 0, 'powerpoint_frames_seen': 0,
            'max_video_width': 0, 'max_video_height': 0,
            'max_rect_width': 0, 'max_rect_height': 0,
            'match_reason': 'no_video',
        }
        powerpoint_frames = []
        stage_candidates = []
        stage_selectors = list(_STAGE_SELECTORS)
        # 1) Single-pass lightweight JS evaluation per frame: WebRTC <video> + stage DOM candidate probe
        for frame in frames:
            if callable(getattr(frame, 'is_detached', None)) and frame.is_detached():
                continue
            agg['frames_checked'] += 1
            if frame is not main_frame and _is_powerpoint_live_url(getattr(frame, 'url', '')):
                agg['powerpoint_frames_seen'] += 1
                powerpoint_frames.append(frame)
            try:
                result = await asyncio.wait_for(
                    frame.evaluate(CAPTURE_SHARE_VIDEO_JS, stage_selectors),
                    timeout=self._evaluate_timeout,
                )
            except Exception as exc:
                if 'outgoing_video_sender_enabled' in str(exc):
                    raise RuntimeError('outgoing_video_sender_enabled') from None
                if agg['match_reason'] == 'no_video':
                    agg['match_reason'] = 'evaluate_error'
                continue
            if not isinstance(result, dict):
                continue
            self._merge_probe(agg, result.get('probe'))
            if result.get('matched') is True and result.get('blank') is not True:
                raw = base64.b64decode(result['data'], validate=True)
                if 0 < len(raw) <= MAX_FRAME_BYTES:
                    dhash = compute_dhash(result['luma'])
                    self._last_probe = agg
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
            stage_idx = result.get('stage_selector_index')
            match_idx = result.get('stage_match_index', 0)
            if type(stage_idx) is int and 0 <= stage_idx < len(_STAGE_SELECTORS) and type(match_idx) is int and match_idx >= 0:
                stage_candidates.append((frame, _STAGE_SELECTORS[stage_idx], match_idx))

        # 2) Verified PowerPoint Live / Office Online child iframe screenshot
        for frame in powerpoint_frames:
            if callable(getattr(frame, 'is_detached', None)) and frame.is_detached():
                continue
            try:
                frame_el = await asyncio.wait_for(frame.frame_element(), timeout=self._evaluate_timeout)
                allowed = await asyncio.wait_for(
                    frame_el.evaluate("el => !el.closest('[role=\"log\"],[role=\"feed\"]')"),
                    timeout=self._evaluate_timeout,
                )
                if allowed is not True:
                    continue
                box = await frame_el.bounding_box()
                if not box or box.get('width', 0) < 160 or box.get('height', 0) < 90:
                    continue
                raw = await frame_el.screenshot(type='jpeg', quality=85, timeout=3000)
            except Exception:
                continue
            if not raw or len(raw) > MAX_FRAME_BYTES:
                continue
            encoded = base64.b64encode(raw).decode('ascii')
            try:
                meta = await asyncio.wait_for(
                    page.evaluate(COMPUTE_IMAGE_LUMA_JS, {'mime': 'image/jpeg', 'data': encoded}),
                    timeout=self._evaluate_timeout,
                )
                dhash = compute_dhash(meta['luma'])
            except Exception:
                continue
            agg['match_reason'] = 'powerpoint_live_frame'
            self._last_probe = agg
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

        # 3) Fallback path: ONLY screenshot a non-iframe stage DOM element already confirmed visible by JS.
        #    If the container embeds any <iframe>, every embedded iframe must pass the strict PowerPoint Live URL gate.
        for frame, selector, match_idx in stage_candidates:
            if callable(getattr(frame, 'is_detached', None)) and frame.is_detached():
                continue
            try:
                locator = frame.locator(selector).nth(match_idx)
                check = await asyncio.wait_for(
                    locator.evaluate(
                        """el => {
                          if (el.tagName === 'IFRAME' || el.closest('[role="log"],[role="feed"]')) {
                            return {allowed: false, iframeCount: 0, shadowIframe: false};
                          }
                          let shadowIframe = false;
                          const stack = [];
                          const visit = (root, depth) => {
                            if (!root || depth > 12) return;
                            for (const node of root.querySelectorAll('*')) {
                              if (node.shadowRoot) {
                                if (node.shadowRoot.querySelector('iframe')) shadowIframe = true;
                                stack.push([node.shadowRoot, depth + 1]);
                              }
                            }
                          };
                          if (el.shadowRoot) {
                            if (el.shadowRoot.querySelector('iframe')) shadowIframe = true;
                            stack.push([el.shadowRoot, 1]);
                          }
                          visit(el, 0);
                          while (stack.length && !shadowIframe) {
                            const [sr, d] = stack.pop();
                            visit(sr, d);
                          }
                          return {
                            allowed: !shadowIframe,
                            iframeCount: el.querySelectorAll('iframe').length,
                            shadowIframe,
                          };
                        }"""
                    ),
                    timeout=self._evaluate_timeout,
                )
                if not isinstance(check, dict) or check.get('allowed') is not True:
                    continue
                iframe_count = check.get('iframeCount')
                if type(iframe_count) is not int or iframe_count < 0:
                    continue
                if iframe_count > 0:
                    nested_ok = True
                    nested_iframes = locator.locator('iframe')
                    for idx in range(iframe_count):
                        handle = await asyncio.wait_for(
                            nested_iframes.nth(idx).element_handle(timeout=1000),
                            timeout=self._evaluate_timeout,
                        )
                        child = await handle.content_frame() if handle is not None else None
                        if child is None or not _is_powerpoint_live_url(getattr(child, 'url', '')):
                            nested_ok = False
                            break
                    if not nested_ok:
                        continue
                raw = await locator.screenshot(type='jpeg', quality=85, timeout=3000)
            except Exception:
                continue
            if not raw or len(raw) > MAX_FRAME_BYTES:
                continue
            encoded = base64.b64encode(raw).decode('ascii')
            try:
                meta = await asyncio.wait_for(
                    page.evaluate(COMPUTE_IMAGE_LUMA_JS, {'mime': 'image/jpeg', 'data': encoded}),
                    timeout=self._evaluate_timeout,
                )
                dhash = compute_dhash(meta['luma'])
            except Exception:
                continue
            agg['match_reason'] = 'stage_dom_candidate'
            self._last_probe = agg
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
        self._last_probe = agg
        return None
