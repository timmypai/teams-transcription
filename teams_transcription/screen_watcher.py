"""Phase 1 (on_demand) & Phase 2 (watch_and_cache) screen-share context controller.

Coordinates Teams screen-share capture, perceptual dHash slide-change detection,
bounded /dev/shm raw frame caching, read-only Codex vision analysis, and
lease-fenced persistence of structured text observations into SQLite.
"""
import asyncio
import contextlib
import inspect
import re
import time

from .screen_capture import ScreenFrameCache, hamming_distance
from .screen_vision import ScreenVisionError

SCREEN_MODES = frozenset({'off', 'on_demand', 'watch_and_cache'})
DEFAULT_CHANGE_HAMMING_THRESHOLD = 6

_DEEP_LOOK_PATTERN = re.compile(
    r'(?:細節|細字|小字|第[一二三四五六七八九十0-9]+[列行頁欄]|上一頁|前一頁|剛剛那[頁張]|'
    r'左邊|右邊|上方|下方|右下|左下|右上|左上|欄位|行號|exact|detail|row|column|previous slide)',
    re.IGNORECASE,
)


def _needs_deep_look(question, cached_record):
    if cached_record is None:
        return False
    if cached_record.get('uncertain_areas'):
        return True
    return bool(isinstance(question, str) and _DEEP_LOOK_PATTERN.search(question))


class ScreenContextController:
    """Manages screen-share visual context in off / on_demand / watch_and_cache mode."""

    def __init__(self, meeting_id, *, mode='off', capturer=None, analyzer=None,
                 persist_observation=None, frame_cache=None,
                 poll_seconds=2.5, min_analyze_interval_seconds=6.0,
                 change_hamming_threshold=DEFAULT_CHANGE_HAMMING_THRESHOLD,
                 max_frames=16, clock=time.monotonic):
        if mode not in SCREEN_MODES:
            raise ValueError('invalid_screen_context_mode')
        self.meeting_id = meeting_id
        self.mode = mode
        self.capturer = capturer
        self.analyzer = analyzer
        self.persist_observation = persist_observation
        self._frame_cache = frame_cache
        self.poll_seconds = poll_seconds
        self.min_analyze_interval_seconds = min_analyze_interval_seconds
        self.change_hamming_threshold = change_hamming_threshold
        self.max_frames = max_frames
        self.clock = clock

        self.started = False
        self._closing = False
        self._watch_task = None
        self._analyze_lock = asyncio.Lock()
        self._in_flight_task = None

        self._capture_seq = 0
        self._share_active = False
        self._last_capture_method = None
        self._last_frame_width = None
        self._last_frame_height = None
        self._last_analyzed_sha = None
        self._last_analyzed_dhash = None
        self._last_analyzed_at = None
        self._analyzed_by_sha = {}  # frame_sha256 -> compact metadata (no raw image)
        self._candidate_frame = None
        self._wake_frame = None

        self._observations_saved = 0
        self._vision_calls = 0
        self._vision_failures = 0
        self._cache_hits = 0
        self._deep_look_calls = 0
        self._last_error_code = None
        self.cleanup_status = 'idle'

    def _ensure_cache(self):
        if self._frame_cache is None:
            self._frame_cache = ScreenFrameCache(max_frames=self.max_frames)
        return self._frame_cache

    async def start(self):
        if self.started or self._closing:
            return False
        self.started = True
        if self.mode != 'off':
            self._ensure_cache()
        if self.mode == 'watch_and_cache' and self.capturer is not None:
            self._watch_task = asyncio.create_task(self._watch_loop())
        return True

    async def _persist(self, observation):
        if self.persist_observation is None:
            return
        result = self.persist_observation(observation)
        if inspect.isawaitable(result):
            await result

    async def _analyze_and_persist(self, frame):
        """Cache raw frame in /dev/shm, analyze with Codex vision, persist text to SQLite."""
        if frame.frame_sha256 in self._analyzed_by_sha:
            return self._analyzed_by_sha[frame.frame_sha256]
        async with self._analyze_lock:
            if frame.frame_sha256 in self._analyzed_by_sha:
                return self._analyzed_by_sha[frame.frame_sha256]
            cache = self._ensure_cache()
            next_seq = self._capture_seq + 1
            cached = cache.put(
                frame.image_bytes,
                capture_seq=next_seq,
                frame_sha256=frame.frame_sha256,
                dhash=frame.dhash,
                width=frame.width,
                height=frame.height,
                capture_method=frame.capture_method,
                captured_at=frame.captured_at,
                mime=frame.mime,
            )
            self._vision_calls += 1
            analysis = await self.analyzer.analyze_frame(cached.path, detail='high')
            self._capture_seq = next_seq
            obs_id = f'screen:{next_seq:04d}:{frame.frame_sha256[:8]}'
            structured = {
                'screen_kind': analysis['screen_kind'],
                'title': analysis['title'],
                'summary_text': analysis['summary_text'],
                'key_items': analysis['key_items'],
                'metrics_or_numbers': analysis['metrics_or_numbers'],
                'visible_text_excerpt': analysis['visible_text_excerpt'],
                'uncertain_areas': analysis['uncertain_areas'],
            }
            record = {
                'observation_id': obs_id,
                'capture_seq': next_seq,
                'frame_sha256': frame.frame_sha256,
                'dhash': frame.dhash,
                'capture_method': frame.capture_method,
                'width': frame.width,
                'height': frame.height,
                'captured_at': frame.captured_at,
                'analyzed_at': analysis['analyzed_at'],
                'vision_model': analysis['vision_model'],
                'screen_kind': analysis['screen_kind'],
                'title': analysis['title'],
                'summary_text': analysis['summary_text'],
                'structured': structured,
            }
            await self._persist(record)
            self._observations_saved += 1
            self._last_analyzed_sha = frame.frame_sha256
            self._last_analyzed_dhash = frame.dhash
            self._last_analyzed_at = self.clock()
            self._last_capture_method = frame.capture_method
            self._last_frame_width = frame.width
            self._last_frame_height = frame.height
            summary_meta = {
                'capture_seq': next_seq,
                'frame_sha256': frame.frame_sha256,
                'dhash': frame.dhash,
                'captured_at': frame.captured_at,
                'analyzed_at_mono': self._last_analyzed_at,
                'summary_text': analysis['summary_text'],
                'uncertain_areas': list(analysis['uncertain_areas']),
            }
            self._analyzed_by_sha[frame.frame_sha256] = summary_meta
            return summary_meta

    async def _watch_loop(self):
        try:
            while self.started and not self._closing:
                try:
                    await self._poll_once()
                except asyncio.CancelledError:
                    raise
                except ScreenVisionError as exc:
                    self._vision_failures += 1
                    self._last_error_code = exc.code
                except Exception:
                    self._vision_failures += 1
                    self._last_error_code = 'screen_watch_failed'
                await asyncio.sleep(self.poll_seconds)
        except asyncio.CancelledError:
            raise

    async def _poll_once(self):
        if self.capturer is None or self.analyzer is None or self._closing:
            return
        frame = await self.capturer.capture()
        if frame is None:
            self._share_active = False
            self._candidate_frame = None
            return
        self._share_active = True
        self._last_capture_method = frame.capture_method
        self._last_frame_width = frame.width
        self._last_frame_height = frame.height

        changed_from_analyzed = (
            self._last_analyzed_dhash is None
            or hamming_distance(frame.dhash, self._last_analyzed_dhash) >= self.change_hamming_threshold
        )
        if not changed_from_analyzed:
            self._candidate_frame = None
            return

        if self._candidate_frame is None:
            self._candidate_frame = frame
            return

        # Confirm the slide has stabilized across two consecutive polls
        if hamming_distance(frame.dhash, self._candidate_frame.dhash) >= self.change_hamming_threshold:
            self._candidate_frame = frame
            return

        now = self.clock()
        if (self._last_analyzed_at is not None
                and now - self._last_analyzed_at < self.min_analyze_interval_seconds):
            self._candidate_frame = frame
            return

        self._candidate_frame = None
        task = asyncio.create_task(self._analyze_and_persist(frame))
        self._in_flight_task = task
        try:
            await task
        finally:
            if self._in_flight_task is task:
                self._in_flight_task = None

    async def note_wake(self):
        """Capture lightweight wake-time frame hint so mid-question flips are preserved."""
        if self.mode == 'off' or not self.started or self._closing or self.capturer is None:
            return None
        try:
            frame = await self.capturer.capture()
            if frame is not None:
                self._wake_frame = frame
                self._share_active = True
            return frame
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    async def resolve_for_question(self, question):
        """Resolve screen-share context automatically for an admitted B question."""
        if self.mode == 'off' or self._closing:
            return None
        if self.capturer is None or self.analyzer is None:
            return {
                'mode': self.mode,
                'available': False,
                'share_active_now': False,
                'reason': 'screen_capture_unconfigured',
            }

        wake_frame, self._wake_frame = self._wake_frame, None
        try:
            # In watch_and_cache mode, join any in-flight background slide analysis first
            if self._in_flight_task is not None and not self._in_flight_task.done():
                await asyncio.shield(self._in_flight_task)

            live_frame = await self.capturer.capture()
            self._share_active = live_frame is not None
            if live_frame is None and wake_frame is None:
                return {
                    'mode': self.mode,
                    'available': self._observations_saved > 0,
                    'share_active_now': False,
                    'current_capture_seq': self._capture_seq or None,
                    'cache_hit': self._observations_saved > 0,
                    'wake_slide_changed': False,
                    'question_focused_vision': None,
                    'reason': 'no_active_screen_share' if self._observations_saved == 0 else 'prior_observations_only',
                }

            wake_slide_changed = False
            if wake_frame is not None and live_frame is not None:
                wake_slide_changed = (
                    hamming_distance(wake_frame.dhash, live_frame.dhash) >= self.change_hamming_threshold
                )
            elif wake_frame is not None and live_frame is None:
                live_frame = wake_frame
                wake_frame = None

            if wake_slide_changed and wake_frame is not None:
                await self._analyze_and_persist(wake_frame)

            target_frame = live_frame
            cache_hit = False
            already_similar = (
                self._last_analyzed_sha is not None
                and (target_frame.frame_sha256 in self._analyzed_by_sha
                     or (self._last_analyzed_dhash is not None
                         and hamming_distance(target_frame.dhash, self._last_analyzed_dhash)
                         < self.change_hamming_threshold))
            )

            if already_similar:
                record_meta = (
                    self._analyzed_by_sha.get(target_frame.frame_sha256)
                    or self._analyzed_by_sha.get(self._last_analyzed_sha)
                )
                cache_hit = True
                self._cache_hits += 1
            else:
                record_meta = await self._analyze_and_persist(target_frame)

            focused = None
            want_focused = (
                self.mode == 'on_demand'
                or _needs_deep_look(question, record_meta)
            )
            if want_focused and record_meta is not None:
                cache = self._ensure_cache()
                cached = cache.get(record_meta['frame_sha256']) or cache.latest()
                if cached is not None:
                    self._deep_look_calls += 1
                    self._vision_calls += 1
                    focused = await self.analyzer.analyze_question(
                        cached.path,
                        question,
                        detail='high',
                        prior_summary=record_meta.get('summary_text'),
                    )

            freshness = (
                max(0.0, round(self.clock() - record_meta['analyzed_at_mono'], 2))
                if record_meta and record_meta.get('analyzed_at_mono') is not None
                else 0.0
            )
            return {
                'mode': self.mode,
                'available': True,
                'share_active_now': self._share_active,
                'current_capture_seq': record_meta['capture_seq'] if record_meta else self._capture_seq,
                'freshness_seconds': freshness,
                'cache_hit': cache_hit,
                'wake_slide_changed': wake_slide_changed,
                'question_focused_vision': focused,
            }
        except asyncio.CancelledError:
            raise
        except ScreenVisionError as exc:
            self._vision_failures += 1
            self._last_error_code = exc.code
            return {
                'mode': self.mode,
                'available': False,
                'share_active_now': self._share_active,
                'current_capture_seq': self._capture_seq or None,
                'error_code': exc.code,
                'question_focused_vision': None,
            }
        except Exception:
            self._vision_failures += 1
            self._last_error_code = 'screen_context_failed'
            return {
                'mode': self.mode,
                'available': False,
                'share_active_now': self._share_active,
                'current_capture_seq': self._capture_seq or None,
                'error_code': 'screen_context_failed',
                'question_focused_vision': None,
            }

    def status(self):
        """Allowlisted diagnostics only: never include OCR text, titles or images."""
        return {
            'mode': self.mode,
            'started': self.started,
            'share_active': self._share_active,
            'capture_method': self._last_capture_method,
            'observations_saved': self._observations_saved,
            'vision_calls': self._vision_calls,
            'vision_failures': self._vision_failures,
            'cache_hits': self._cache_hits,
            'deep_look_calls': self._deep_look_calls,
            'last_error_code': self._last_error_code,
            'last_capture_seq': self._capture_seq,
            'last_frame_width': self._last_frame_width,
            'last_frame_height': self._last_frame_height,
            'cache': self._frame_cache.status() if self._frame_cache is not None else None,
            'cleanup_status': self.cleanup_status,
        }

    async def close(self):
        self._closing = True
        self.started = False
        self.cleanup_status = 'cleaning'
        errors = []
        tasks = [t for t in (self._watch_task, self._in_flight_task)
                 if t is not None and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._watch_task = None
        self._in_flight_task = None
        if self.analyzer is not None:
            try:
                await self.analyzer.close()
            except Exception:
                errors.append('vision_close_failed')
        if self._frame_cache is not None:
            try:
                if not self._frame_cache.close():
                    errors.append('frame_cache_close_failed')
            except Exception:
                errors.append('frame_cache_close_failed')
        self.cleanup_status = 'unknown' if errors else 'cleaned'
        if errors:
            self._last_error_code = errors[0]
            raise RuntimeError('screen_cleanup_unknown')
        return self.cleanup_status
