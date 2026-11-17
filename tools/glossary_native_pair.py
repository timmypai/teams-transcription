"""DI-only paired A glossary runner; no CLI, model grant or ledger ownership.

The trusted caller must reserve TWO canonical slots before calling run_pair.
budget.claim(slot, stage) durably fences the existing thread/realtime stages;
this module never reserves, refunds, retries, resets or settles those slots.
Factory/session implementations are trusted in-process code, not JSON receipts.
The actual LiveProvider adapter checks its owned cleanup API before start.
The root QA mode/authority integration is still pending; no CLI enables live.
"""
import asyncio
import base64
import copy
from dataclasses import dataclass
from enum import Enum
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import re
import stat
import uuid
import wave

from teams_transcription.glossary_context import glossary_initial_items


MODEL = 'gpt-live-1-codex'
ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FAILURES = {'budget_claim_denied', 'budget_stage_invalid', 'session_reuse_rejected',
    'native_stage_evidence_missing', 'capture_result_invalid', 'capture_unverified',
    'owned_cleanup_api_missing', 'native_rtp_evidence_missing', 'native_source_reuse',
    'native_source_unavailable', 'native_source_incomplete', 'native_source_scope_mismatch',
    'native_source_cleanup_unknown', 'private_output_unavailable', 'private_output_invalid'}


class PairError(ValueError):
    """Fixed codes only; never expose provider/file exception bodies."""


class CleanupStatus(Enum):
    CLEAN = 'owned_cleanup_verified'
    UNKNOWN = 'owned_cleanup_unknown'


@dataclass(frozen=True)
class AudioStimulus:
    pcm: bytes
    wav_sha256: str
    pcm_sha256: str
    frames: int
    sample_rate: int = 24000


@dataclass(frozen=True)
class CaptureResult:
    text: str
    delivery_verified: bool
    model_error: bool
    raw_final_verified: bool = True
    delivery_basis: str = 'trusted_adapter_attestation'


def _require(condition, code):
    if not condition:
        raise PairError(code)


def _failure(error):
    return str(error) if isinstance(error, PairError) and str(error) in PUBLIC_FAILURES else 'native_operation_unknown'


def _private(record, maximum=262144):
    try:
        _require(isinstance(record, dict) and isinstance(record.get('path'), str)
            and re.fullmatch('[0-9a-f]{64}', str(record.get('sha256'))), 'private_input_invalid')
        path = Path(record['path'])
        _require(not path.resolve().is_relative_to(ROOT), 'private_input_in_repository')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_mode & 0o077 == 0 and info.st_size <= maximum, 'private_input_invalid')
            raw = stream.read(maximum + 1)
        _require(len(raw) <= maximum and hashlib.sha256(raw).hexdigest() == record['sha256'],
            'private_input_identity_mismatch')
        return raw
    except OSError:
        raise PairError('private_input_unavailable') from None


def _write(path, raw):
    try:
        with os.fdopen(_open_output(path), 'wb') as stream:
            stream.write(raw)
    except OSError:
        raise PairError('private_output_unavailable') from None
    return hashlib.sha256(raw).hexdigest()


def _open_output(path):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError:
        raise PairError('private_output_unavailable') from None
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and info.st_mode & 0o077 == 0, 'private_output_invalid')
        os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _json(path, value):
    return _write(path, (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode())


def _audio(record):
    raw = _private(record, 256 * 1024 * 1024)
    try:
        with wave.open(io.BytesIO(raw), 'rb') as stream:
            _require(stream.getnchannels() == 1 and stream.getsampwidth() == 2
                and stream.getframerate() == 24000 and stream.getcomptype() == 'NONE'
                and stream.getnframes() > 0, 'audio_format_invalid')
            frames = stream.getnframes()
            pcm = stream.readframes(frames)
            _require(len(pcm) == frames * 2, 'audio_format_invalid')
    except (wave.Error, EOFError):
        raise PairError('audio_format_invalid') from None
    return AudioStimulus(pcm, record['sha256'], hashlib.sha256(pcm).hexdigest(), frames)


class _StageFence:
    def __init__(self, budget, slot):
        self.budget, self.slot, self.stages, self.active = budget, slot, [], True

    def __call__(self, stage):
        _require(self.active and self.stages == ([] if stage == 'thread' else ['thread'])
            and stage in ('thread', 'realtime'), 'budget_stage_invalid')
        try:
            result = self.budget.claim(self.slot, stage)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise PairError('budget_claim_denied')
            _require(result is not False, 'budget_claim_denied')
        except Exception:
            raise PairError('budget_claim_denied') from None
        self.stages.append(stage)


_METHODS = {'thread/realtime/started', 'thread/realtime/transcript/delta',
    'thread/realtime/transcript/done', 'thread/realtime/item/started',
    'thread/realtime/item/completed', 'thread/realtime/item/transcript/delta'}


def _original_record(value):
    """Preserve provider text/version; exclude account/error/token payloads."""
    if not isinstance(value, dict) or value.get('method') not in _METHODS:
        return None
    params = value.get('params')
    if not isinstance(params, dict):
        return None
    allowed = {'threadId', 'realtimeSessionId', 'version', 'itemId', 'role', 'text', 'delta'}
    data = {key: item for key, item in params.items() if key in allowed and isinstance(item, str)}
    if type(params.get('revision')) is int:
        data['revision'] = params['revision']
    if isinstance(params.get('item'), dict):
        data['item'] = {key: item for key, item in params['item'].items()
            if key in {'id', 'type', 'role', 'text', 'realtimeSessionId'} and isinstance(item, str)}
        if type(params['item'].get('revision')) is int:
            data['item']['revision'] = params['item']['revision']
    return {'schema_version': 1, 'representation': 'provider_original',
        'method': value['method'], 'params': data}


async def run_pair(*, audio, reference, glossary, slots, budget, factory, private_dir,
                   annotations, dataset_kind='synthetic'):
    """Run exactly two trusted sessions in order; return hashes/fixed labels only.

    factory receives no reference/annotations. Its start MUST call the supplied
    synchronous before_native_start hook immediately before each native stage.
    close_and_verify MUST inspect owned resources, returning CleanupStatus.CLEAN;
    JSON/bool receipts never authorize the next session. Live bridge: pending.
    """
    _require(type(slots) in (tuple, list) and len(slots) == 2 and slots[0] != slots[1]
        and all(type(slot) is int and 1 <= slot <= 50 for slot in slots)
        and callable(getattr(budget, 'claim', None)) and callable(factory), 'pair_budget_invalid')
    _require(isinstance(reference, dict) and reference.get('human_reviewed') is True,
        'reference_review_missing')
    stimulus = _audio(audio)
    try:
        _private(reference).decode('utf-8')
        snapshot = json.loads(_private(glossary))
        initial = glossary_initial_items(snapshot)
    except (UnicodeError, ValueError) as error:
        raise PairError(str(error) if isinstance(error, PairError) else 'glossary_input_invalid') from None
    _require(type(glossary.get('source_version')) is int
        and glossary['source_version'] == snapshot['version'] and initial, 'glossary_identity_invalid')
    _require(dataset_kind in ('synthetic', 'human'), 'dataset_kind_invalid')
    out = Path(private_dir)
    _require(not out.resolve().is_relative_to(ROOT), 'private_output_in_repository')
    try:
        out.mkdir(mode=0o700, exist_ok=True)
        info = out.lstat()
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and info.st_mode & 0o077 == 0, 'private_output_invalid')
    except OSError:
        raise PairError('private_output_unavailable') from None
    _json(out / 'glossary-snapshot.json', snapshot)
    summary = {'schema_version': 1, 'status': 'incomplete', 'failure_code': None,
        'audio_sha256': stimulus.wav_sha256, 'pcm_sha256': stimulus.pcm_sha256,
        'reference_sha256': reference['sha256'], 'glossary_sha256': glossary['sha256'],
        'applied_snapshot_sha256': snapshot['sha256'], 'glossary_source_version': snapshot['version'],
        'reserved_slots_required': 2, 'ledger_owned_by_runner': False,
        'budget_claims_delegated': True, 'runs': [],
        'model_consumption_acknowledged': False,
        'live_entrypoint': 'pending_root_qa_mode_and_fixed_installation'}
    outputs, sessions, cancelled = {}, [], None
    for index, mode in enumerate(('baseline', 'glossary')):
        fence = _StageFence(budget, slots[index])
        session = None
        raw_path = out / (mode + '-events.jsonl')
        fd = _open_output(raw_path)
        record_count, record_open, digest = 0, True, hashlib.sha256()
        with os.fdopen(fd, 'wb') as raw_stream:
            def record(value):
                nonlocal record_count
                value = _original_record(value)
                if record_open and value is not None:
                    raw = (json.dumps(value, ensure_ascii=False) + '\n').encode()
                    raw_stream.write(raw); digest.update(raw)
                    record_count += 1
            cleanup = CleanupStatus.UNKNOWN
            failure = result = None
            try:
                state_dir = out / (mode + '-runtime')
                state_dir.mkdir(mode=0o700)
                candidate = factory(audio=stimulus, glossary_snapshot=copy.deepcopy(snapshot) if index else None,
                    state_dir=state_dir, before_native_start=fence, record=record)
                _require(all(candidate is not old for old in sessions), 'session_reuse_rejected')
                session = candidate
                sessions.append(session)
                await session.start()
                _require(fence.stages == ['thread', 'realtime'], 'native_stage_evidence_missing')
                result = await session.capture()
            except BaseException as error:
                failure = _failure(error)
                if isinstance(error, asyncio.CancelledError):
                    cancelled = error
            finally:
                fence.active = False
                if session is not None:
                    try:
                        cleanup = await session.close_and_verify()
                    except BaseException as error:
                        cleanup = CleanupStatus.UNKNOWN
                        if isinstance(error, asyncio.CancelledError):
                            cancelled = error
            # Keep callbacks alive through close; no provider text is exported
            # until the owned receiver has had its final cleanup opportunity.
            try:
                receipt = getattr(session, 'cleanup_receipt', None)
                if isinstance(receipt, dict):
                    fields = ('close_requested', 'stop_rpc_acknowledged', 'owned_session_closed',
                        'peer_closed', 'rpc_reaped', 'receiver_done', 'rpc_tasks_done', 'source_closed',
                        'resources_closed', 'cleanup_failed', 'cleanup_verified')
                    _json(out / (mode + '-cleanup.json'), {'version': 1,
                        **{key: receipt.get(key) is True for key in fields}})
                if callable(getattr(session, 'capture_result', None)):
                    result = session.capture_result()
                if result is not None:
                    _require(type(result) is CaptureResult and isinstance(result.text, str)
                        and type(result.delivery_verified) is bool and type(result.model_error) is bool,
                        'capture_result_invalid')
                    _require(result.delivery_basis in {'trusted_adapter_attestation',
                        'native_buffer_end_and_outbound_rtp'}, 'capture_result_invalid')
                    text_path = out / (mode + '-provider.txt')
                    text_sha = _write(text_path, result.text.encode())
                    output = dict(path=str(text_path), sha256=text_sha, audio_sha256=stimulus.wav_sha256,
                        provider='codex_live', model=MODEL,
                        representation='provider_original' if result.raw_final_verified is True else 'unverified_events_only',
                        raw_final_verified=result.raw_final_verified is True,
                        delivery_verified=result.delivery_verified, model_error=result.model_error,
                        delivery_basis=result.delivery_basis, model_consumption_acknowledged=False,
                        glossary_applied=bool(index))
                    if index:
                        output.update(glossary_sha256=glossary['sha256'], glossary_source_version=snapshot['version'],
                            applied_snapshot_sha256=snapshot['sha256'])
                    outputs['with_glossary' if index else 'without_glossary'] = output
                    _require(result.delivery_verified and not result.model_error
                        and result.raw_final_verified is True, 'capture_unverified')
            except Exception as error:
                failure = failure or _failure(error)
            record_open = False
            summary['runs'].append({'mode': mode, 'thread_claimed': 'thread' in fence.stages,
                'realtime_claimed': 'realtime' in fence.stages, 'provider_records': record_count,
                'provider_events_sha256': digest.hexdigest(), 'cleanup_verified': cleanup is CleanupStatus.CLEAN})
            if failure is not None or cleanup is not CleanupStatus.CLEAN:
                summary.update(status='failed' if cleanup is CleanupStatus.CLEAN else 'cleanup_unknown',
                    failure_code=failure or 'owned_cleanup_unknown')
                break
    if len(outputs) == 2 and all(row['cleanup_verified'] for row in summary['runs']) and summary['failure_code'] is None:
        summary['status'] = 'complete'
    manifest = dict(schema_version=1, normalization='nfkc-v1', dataset_kind=dataset_kind,
        audio=copy.deepcopy(audio), glossary=copy.deepcopy(glossary), cases=[{
            'truth_ref': copy.deepcopy(reference), 'annotations': copy.deepcopy(annotations), **outputs}])
    _json(out / 'scoring-manifest.json', manifest)
    _json(out / 'scoring-status.json', {'status': 'pending_offline_evaluation',
        'semantic_assessment': 'not_performed'})
    _json(out / 'pair-summary.json', summary)
    if cancelled is not None:
        raise cancelled
    return summary


NATIVE_SOURCE_JS = r"""({owner,pcm,frames})=>{
  const live=window.__live;
  if(!live||live.inputMode!=='native_media'||live.ctx?.sampleRate!==24000||
      live.ctx.state!=='running'||!live.destination||live.glossaryPairSource)
    throw Error('pair_native_source_unavailable');
  const bytes=Uint8Array.from(atob(pcm),c=>c.charCodeAt(0));
  if(bytes.length!==frames*2)throw Error('pair_native_source_format');
  const view=new DataView(bytes.buffer),buffer=live.ctx.createBuffer(1,frames,24000);
  const data=buffer.getChannelData(0);
  for(let i=0;i<frames;i++)data[i]=view.getInt16(i*2,true)/32768;
  bytes.fill(0);
  const node=live.ctx.createBufferSource();node.buffer=buffer;
  node.connect(live.destination); // RTC input only; never ctx.destination.
  live.glossaryPairSource={owner,live,node,frames,started:false,ended:false,closed:false};
  return true;
}"""

PLAY_NATIVE_SOURCE_JS = r"""owner=>{
  const live=window.__live,state=live?.glossaryPairSource;
  if(!state||state.owner!==owner||state.live!==live||state.started||state.closed||
      live.ctx.state!=='running'||live.pc.connectionState!=='connected'||live.dc.readyState!=='open')
    throw Error('pair_native_source_unavailable');
  state.started=true;
  return new Promise(resolve=>{
    state.finish=resolve;
    state.node.onended=()=>{state.ended=true;resolve({completed:!state.closed,frames:state.frames});};
    state.node.start();
  });
}"""

STOP_NATIVE_SOURCE_JS = r"""owner=>{
  const live=window.__live,state=live?.glossaryPairSource;
  if(!state||state.owner!==owner||state.live!==live)return false;
  if(state.closed)return true;
  state.closed=true;state.node.onended=null;
  if(state.started&&!state.ended)try{state.node.stop();}catch{}
  state.node.disconnect();state.node.buffer=null;
  state.finish?.({completed:false,frames:state.frames});return true;
}"""


class NativeWavSource:
    """Single fixed native buffer in the model's private page; no PCM push."""
    def __init__(self, audio):
        self.audio, self.owner = audio, uuid.uuid4().hex
        self.page = None
        self.native_audio_failed = False
        self.stopped = False

    async def connect_native_audio(self, page):
        _require(self.page is None and not self.stopped, 'native_source_reuse')
        self.page = page
        result = await page.evaluate(NATIVE_SOURCE_JS, {'owner': self.owner,
            'pcm': base64.b64encode(self.audio.pcm).decode(), 'frames': self.audio.frames})
        _require(result is True, 'native_source_unavailable')

    async def play(self):
        _require(self.page is not None and not self.stopped, 'native_source_unavailable')
        value = await self.page.evaluate(PLAY_NATIVE_SOURCE_JS, self.owner)
        _require(isinstance(value, dict) and value.get('completed') is True
            and value.get('frames') == self.audio.frames, 'native_source_incomplete')
        return value

    async def native_audio_readiness(self):
        return {'ready': self.page is not None and not self.stopped and not self.native_audio_failed,
            'failed': self.native_audio_failed, 'model_consumption_verified': False}

    async def disconnect_native_audio(self, page):
        if self.stopped:
            return
        _require(page is self.page, 'native_source_scope_mismatch')
        if not page.is_closed():
            _require(await page.evaluate(STOP_NATIVE_SOURCE_JS, self.owner) is True,
                'native_source_cleanup_unknown')
        self.stopped = True

    async def stop(self):
        if self.page is not None:
            await self.disconnect_native_audio(self.page)


class DefaultLiveAdapter:
    """Actual fixed A provider; only the future trusted root QA mode may call.

    delivery_verified describes owned native-buffer completion plus outbound
    RTP, never a model-consumption ACK. No application summary probe is used.
    """
    blockers = ('root_qa_mode_missing', 'fixed_host_installation_pending',
        'native_source_actual_browser_validation_pending')

    def __init__(self, *, browser, audio, glossary_snapshot, state_dir,
                 before_native_start, record, executable='codex',
                 provider_factory=None, source_factory=NativeWavSource):
        from teams_transcription.live import LiveProvider
        self.source = source_factory(audio)
        self.raw_session = self.raw_mode = None
        self.raw_order, self.raw_items, self.raw_ambiguous = [], {}, False
        self.delivery = False
        self.operation_failed = False
        self.cleaned = None
        async def ignored(*args):
            pass
        self.provider = (provider_factory or LiveProvider)(browser, state_dir, ignored, ignored, executable,
            media_source=self.source, glossary_snapshot=copy.deepcopy(glossary_snapshot),
            before_native_start=before_native_start)
        previous = self.provider.rpc.on_notification
        async def ingress(value):
            params = value.get('params') if isinstance(value, dict) else None
            try:
                if previous is not None:
                    await previous(value)
            finally:
                if (isinstance(self.provider.thread_id, str) and self.provider.thread_id
                        and isinstance(params, dict) and params.get('threadId') == self.provider.thread_id):
                    self._raw_final(value['method'], params)
                    record(value)
        self.provider.rpc.on_notification = ingress

    def _raw_final(self, method, params):
        if method == 'thread/realtime/started':
            session = params.get('realtimeSessionId')
            if params.get('version') == 'v3' and isinstance(session, str) and session:
                if self.raw_session not in (None, session):
                    self.raw_ambiguous = True
                self.raw_session = session
            return
        canonical = method in {'thread/realtime/item/started', 'thread/realtime/item/completed'}
        if canonical:
            item = params.get('item')
            if not isinstance(item, dict) or item.get('type') != 'transcriptSegment' or item.get('role') != 'user':
                return
            if self.raw_session is None or item.get('realtimeSessionId') != self.raw_session:
                self.raw_ambiguous = True; return
            if self.raw_mode == 'flat':
                self.raw_ambiguous = True; return
            self.raw_mode = 'canonical'
            ident, text, revision = item.get('id'), item.get('text'), item.get('revision')
        elif method == 'thread/realtime/transcript/done' and params.get('role') == 'user':
            if self.raw_mode == 'canonical':
                return  # Raw mirror remains in the private event history.
            self.raw_mode = 'flat'
            ident, text, revision = params.get('itemId'), params.get('text'), params.get('revision')
        else:
            return
        if not isinstance(ident, str) or not ident or len(ident) > 256:
            self.raw_ambiguous = True; return
        if ident not in self.raw_order:
            self.raw_order.append(ident)
        if method.endswith('/started'):
            return
        if not isinstance(text, str) or revision is not None and (type(revision) is not int or revision < 0):
            self.raw_ambiguous = True; return
        previous = self.raw_items.get(ident)
        if previous is not None and previous != (text, revision):
            if previous[1] is None or revision is None or revision <= previous[1]:
                self.raw_ambiguous = True; return
        self.raw_items[ident] = (text, revision)

    async def start(self):
        _require(callable(getattr(self.provider, 'cleanup_snapshot', None)), 'owned_cleanup_api_missing')
        await self.provider.start()

    @staticmethod
    def _sent(value):
        rows = value.get('reports') if isinstance(value, dict) else None
        _require(isinstance(rows, list) and rows, 'native_rtp_evidence_missing')
        counts = [row.get('packetsSent') for row in rows if isinstance(row, dict)]
        _require(len(counts) == len(rows) and all(type(item) is int and item >= 0 for item in counts),
            'native_rtp_evidence_missing')
        return sum(counts)

    async def capture(self):
        try:
            before = self._sent(await self.provider.peer.stats())
            await self.source.play()
            after = self._sent(await self.provider.peer.stats())
            self.delivery = after > before
            await self.source.stop()
            await self.provider.drain()  # Existing A drain/close protocol, not a meeting timer.
            return self.capture_result()
        except BaseException:
            self.operation_failed = True
            raise

    def capture_result(self):
        verified = bool(self.raw_order) and not self.raw_ambiguous and all(key in self.raw_items for key in self.raw_order)
        # Concatenate original characters only; never strip, normalize, invent
        # separators or fill absent finals from deltas. Full history stays raw.
        text = ''.join(self.raw_items[key][0] for key in self.raw_order if key in self.raw_items)
        return CaptureResult(text, self.delivery,
            self.operation_failed or bool(self.provider.error), verified,
            'native_buffer_end_and_outbound_rtp')

    async def close_and_verify(self):
        if self.cleaned is not None:
            return self.cleaned
        source_closed = close_failed = False
        try:
            try:
                await self.source.stop()
                source_closed = True
            finally:
                await self.provider.close()
        except asyncio.CancelledError:
            self.operation_failed = True
            raise
        except Exception:
            self.operation_failed = True
            close_failed = True
        receipt = self.provider.cleanup_snapshot() if callable(getattr(self.provider, 'cleanup_snapshot', None)) else None
        flags = ('close_requested', 'stop_rpc_acknowledged', 'owned_session_closed',
            'peer_closed', 'rpc_reaped', 'receiver_done', 'rpc_tasks_done')
        valid = (source_closed and not close_failed and isinstance(receipt, dict) and receipt.get('version') == 1
            and receipt.get('cleanup_verified') is True and receipt.get('resources_closed') is True
            and receipt.get('cleanup_failed') is False and all(receipt.get(key) is True for key in flags))
        self.cleanup_receipt = {'version': 1, 'source_closed': source_closed,
            **{key: isinstance(receipt, dict) and receipt.get(key) is True for key in flags},
            'resources_closed': isinstance(receipt, dict) and receipt.get('resources_closed') is True,
            'cleanup_failed': close_failed or not isinstance(receipt, dict) or receipt.get('cleanup_failed') is not False,
            'cleanup_verified': valid}
        self.cleaned = CleanupStatus.CLEAN if valid else CleanupStatus.UNKNOWN
        return self.cleaned
