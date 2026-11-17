"""No model, browser, network, credentials or private source glossary."""
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock
import wave

from teams_transcription.meeting_store import canonical_json
from tools.glossary_native_pair import AudioStimulus, CaptureResult, CleanupStatus, DefaultLiveAdapter, NativeWavSource, PairError, run_pair


class GlossaryNativePairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        wav = io.BytesIO()
        with wave.open(wav, 'wb') as stream:
            stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\x10\x00' * 2400)
        self.audio = self.file('audio.wav', wav.getvalue())
        self.reference = {**self.file('truth.txt', b'PRIVATE_REFERENCE_NOT_A_PROMPT'), 'human_reviewed': True}
        entries = [dict(id='a' * 32, kind='term', canonical='PRIVATE_TERM', aliases=[],
            pronunciation='', definition='', revision=1)]
        self.snapshot = dict(version=7, sha256=hashlib.sha256(canonical_json(entries).encode()).hexdigest(), entries=entries)
        self.glossary = {**self.file('glossary.json', json.dumps(self.snapshot).encode()), 'source_version': 7}
        self.order, self.created, self.claims = [], [], []
        outer = self
        class Budget:
            def claim(self, slot, stage):
                outer.claims.append((slot, stage))
        self.budget = Budget()

    def file(self, name, raw):
        path = self.root / name
        path.write_bytes(raw); path.chmod(0o600)
        return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}

    def factory(self, **kwargs):
        number = len(self.created)
        self.created.append(kwargs)
        outer = self
        class Session:
            async def start(self):
                kwargs['before_native_start']('thread')
                outer.order.append(('model', number))
                kwargs['before_native_start']('realtime')
            async def capture(self):
                kwargs['record']({'method': 'thread/realtime/transcript/done',
                    'params': {'role': 'user', 'text': 'PRIVATE_PROVIDER_BODY', 'version': 'v3', 'token': 'DO_NOT_SAVE'}})
                return CaptureResult('PRIVATE_PROVIDER_BODY', delivery_verified=True, model_error=False)
            async def close_and_verify(self):
                outer.order.append(('cleaned', number))
                return CleanupStatus.CLEAN
        return Session()

    async def pair(self, **overrides):
        options = dict(audio=self.audio, reference=self.reference, glossary=self.glossary,
            slots=(17, 18), budget=self.budget, factory=self.factory,
            private_dir=self.root / 'results', annotations=[])
        options.update(overrides)
        return await run_pair(**options)

    async def test_same_audio_and_clean_sessions_with_no_reference_injection(self):
        summary = await self.pair()
        self.assertEqual(summary['status'], 'complete')
        self.assertEqual(self.order, [('model', 0), ('cleaned', 0), ('model', 1), ('cleaned', 1)])
        self.assertEqual(self.claims, [(17, 'thread'), (17, 'realtime'), (18, 'thread'), (18, 'realtime')])
        self.assertIs(self.created[0]['audio'], self.created[1]['audio'])
        self.assertIsNone(self.created[0]['glossary_snapshot'])
        self.assertEqual(self.created[1]['glossary_snapshot'], self.snapshot)
        self.assertNotIn('reference', self.created[0])
        self.assertNotIn('PRIVATE_REFERENCE', repr(self.created))
        self.assertNotEqual(self.glossary['sha256'], self.snapshot['sha256'])
        manifest = json.loads((self.root / 'results' / 'scoring-manifest.json').read_text())
        output = manifest['cases'][0]['with_glossary']
        self.assertEqual(output['glossary_sha256'], self.glossary['sha256'])
        self.assertEqual(output['applied_snapshot_sha256'], self.snapshot['sha256'])

    async def test_cleanup_unknown_prevents_second_session_without_refund(self):
        def factory(**kwargs):
            session = self.factory(**kwargs)
            async def unknown():
                return CleanupStatus.UNKNOWN
            session.close_and_verify = unknown
            return session
        summary = await self.pair(factory=factory)
        self.assertEqual(summary['status'], 'cleanup_unknown')
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.claims, [(17, 'thread'), (17, 'realtime')])

    async def test_budget_denied_before_native_start_never_starts_model(self):
        class Denied:
            def claim(self, slot, stage):
                raise RuntimeError('PRIVATE_TOKEN')
        summary = await self.pair(budget=Denied())
        self.assertEqual(summary['failure_code'], 'budget_claim_denied')
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.order, [('cleaned', 0)])
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(summary))

    async def test_missing_duplicate_or_outside_fifty_slots_prevents_any_session(self):
        for slots in ((17,), (17, 17), (17, 51), (True, 18)):
            with self.subTest(slots=slots):
                with self.assertRaisesRegex(PairError, 'pair_budget_invalid'):
                    await self.pair(slots=slots)
        self.assertEqual(self.created, [])
        self.assertEqual(self.claims, [])

    async def test_private_original_and_public_projection_are_separate(self):
        summary = await self.pair()
        self.assertNotIn('PRIVATE_', json.dumps(summary))
        self.assertNotIn(str(self.root), json.dumps(summary))
        raw = (self.root / 'results' / 'baseline-events.jsonl').read_text()
        self.assertIn('PRIVATE_PROVIDER_BODY', raw)
        self.assertNotIn('DO_NOT_SAVE', raw)
        self.assertEqual((self.root / 'results' / 'baseline-provider.txt').read_text(), 'PRIVATE_PROVIDER_BODY')
        for path in (self.root / 'results').glob('*'):
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    async def test_json_cleanup_receipt_cannot_authorize_second_model(self):
        def factory(**kwargs):
            session = self.factory(**kwargs)
            async def forged():
                return {'cleanup_verified': True, 'stop_rpc_acknowledged': True}
            session.close_and_verify = forged
            return session
        result = await self.pair(factory=factory)
        self.assertEqual(result['status'], 'cleanup_unknown')
        self.assertEqual(len(self.created), 1)

    async def test_private_fifo_and_hash_mismatch_fail_before_any_session(self):
        fifo = self.root / 'fifo'
        os.mkfifo(fifo, 0o600)
        for audio in ({'path': str(fifo), 'sha256': 'a' * 64}, {**self.audio, 'sha256': '0' * 64}):
            with self.subTest(path=Path(audio['path']).name):
                with self.assertRaises(PairError):
                    await self.pair(audio=audio)
        self.assertEqual(self.created, [])
        self.assertEqual(self.claims, [])

    async def test_stage_retries_and_secret_exception_cannot_escape(self):
        def factory(**kwargs):
            session = self.factory(**kwargs)
            async def retry():
                kwargs['before_native_start']('thread')
                kwargs['before_native_start']('thread')
            session.start = retry
            return session
        result = await self.pair(factory=factory)
        self.assertEqual(result['failure_code'], 'budget_stage_invalid')
        self.assertEqual(self.claims, [(17, 'thread')])
        self.assertEqual(len(self.created), 1)

    async def test_arbitrary_pair_error_body_never_reaches_public_summary(self):
        def factory(**kwargs):
            session = self.factory(**kwargs)
            async def fail():
                raise PairError('PRIVATE_TOKEN_PATH')
            session.start = fail
            return session
        result = await self.pair(factory=factory)
        self.assertEqual(result['failure_code'], 'native_operation_unknown')
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(result))

    async def test_export_waits_until_owned_cleanup_has_processed_final_tail(self):
        def factory(**kwargs):
            session = self.factory(**kwargs)
            session.text = 'first'
            async def close():
                session.text = ' first final tail \n'
                return CleanupStatus.CLEAN
            session.close_and_verify = close
            session.capture_result = lambda: CaptureResult(session.text, True, False)
            return session
        result = await self.pair(factory=factory)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual((self.root / 'results' / 'baseline-provider.txt').read_text(), ' first final tail \n')

    def adapter(self):
        return DefaultLiveAdapter(browser=None, audio=None, glossary_snapshot=self.snapshot,
            state_dir=self.root / 'adapter', before_native_start=lambda stage: None,
            record=lambda value: None, source_factory=lambda audio: None)

    async def test_actual_adapter_keeps_raw_whitespace_revision_and_ignores_flat_mirror(self):
        adapter = self.adapter()
        adapter.provider.thread_id = 'owned-thread'
        async def send(method, **params):
            await adapter.provider.rpc.on_notification({'method': method,
                'params': {'threadId': 'owned-thread', **params}})
        await send('thread/realtime/started', version='v3', realtimeSessionId='owned-session')
        item = dict(type='transcriptSegment', id='owned-item', role='user', realtimeSessionId='owned-session')
        await send('thread/realtime/item/started', item={**item, 'text': ''})
        await send('thread/realtime/item/completed', item={**item, 'revision': 1, 'text': ' \n阿體歐\t '})
        await send('thread/realtime/transcript/done', role='user', itemId='owned-item', text='阿體歐')
        self.assertEqual(adapter.capture_result().text, ' \n阿體歐\t ')
        self.assertTrue(adapter.capture_result().raw_final_verified)
        await send('thread/realtime/item/completed', item={**item, 'revision': 2, 'text': ' 重新修訂\t'})
        self.assertEqual(adapter.capture_result().text, ' 重新修訂\t')
        self.assertTrue(adapter.capture_result().raw_final_verified)

    async def test_no_final_or_conflicting_unversioned_id_is_unknown(self):
        adapter = self.adapter()
        adapter.provider.thread_id = 'own'
        async def send(text):
            await adapter.provider.rpc.on_notification({'method': 'thread/realtime/transcript/done',
                'params': {'threadId': 'own', 'role': 'user', 'itemId': 'same-item', 'text': text}})
        self.assertFalse(adapter.capture_result().raw_final_verified)
        await send(' 第一版 ')
        self.assertTrue(adapter.capture_result().raw_final_verified)
        await send(' 未標示版本的另一版 ')
        self.assertFalse(adapter.capture_result().raw_final_verified)
        self.assertEqual(adapter.capture_result().text, ' 第一版 ')

    async def test_actual_adapter_missing_cleanup_api_prevents_sdk_start(self):
        adapter = self.adapter()
        adapter.provider.cleanup_snapshot = None
        adapter.provider.start = AsyncMock()
        with self.assertRaisesRegex(PairError, 'owned_cleanup_api_missing'):
            await adapter.start()
        adapter.provider.start.assert_not_awaited()

    async def test_unbound_or_foreign_raw_thread_cannot_seed_provider_text(self):
        adapter = self.adapter()
        for own, incoming in ((None, None), ('owned', 'foreign')):
            adapter.provider.thread_id = own
            await adapter.provider.rpc.on_notification({'method': 'thread/realtime/transcript/done',
                'params': {'threadId': incoming, 'role': 'user', 'itemId': 'foreign-item', 'text': 'FOREIGN_RAW'}})
        self.assertFalse(adapter.capture_result().raw_final_verified)
        self.assertEqual(adapter.capture_result().text, '')

    async def test_real_evaluator_accepts_pair_file_hashes_without_reference_injection(self):
        from tools.glossary_asr_eval import evaluate_manifest
        reference = Path(self.reference['path']).read_text()
        result = await self.pair(annotations=[{'category': 'ordinary_negative', 'start': 0, 'end': len(reference)}])
        self.assertEqual(result['status'], 'complete')
        manifest = json.loads((self.root / 'results' / 'scoring-manifest.json').read_text())
        report = evaluate_manifest(manifest, base_dir=self.root)
        self.assertEqual(report['status'], 'scored')
        self.assertEqual(report['glossary_sha256'], self.glossary['sha256'])
        self.assertNotIn('PRIVATE_', json.dumps(report))

    async def test_actual_adapter_requires_every_owned_cleanup_flag_not_send_return(self):
        flags = dict.fromkeys(('close_requested', 'stop_rpc_acknowledged', 'owned_session_closed',
            'peer_closed', 'rpc_reaped', 'receiver_done', 'rpc_tasks_done'), True)
        for missing in (None, *flags, 'resources_closed', 'cleanup_failed', 'close_exception'):
            with self.subTest(missing=missing):
                adapter = self.adapter()
                adapter.source = SimpleNamespace(stop=AsyncMock())
                adapter.provider.close = AsyncMock(side_effect=RuntimeError('PRIVATE_CLOSE_ERROR')
                    if missing == 'close_exception' else None)
                adapter.provider.cleanup_snapshot = lambda: {'version': 1, 'cleanup_verified': True,
                    'resources_closed': True, 'cleanup_failed': False, **flags,
                    **({missing: missing == 'cleanup_failed'} if missing else {})}
                expected = CleanupStatus.UNKNOWN if missing else CleanupStatus.CLEAN
                self.assertIs(await adapter.close_and_verify(), expected)
                self.assertIs(await adapter.close_and_verify(), expected)
                adapter.provider.close.assert_awaited_once()

    async def test_native_source_routes_same_pcm_only_to_owned_rtc_input(self):
        stimulus = AudioStimulus(b'\x00\x01' * 24, '0' * 64, '1' * 64, 24)
        source = NativeWavSource(stimulus)
        page = SimpleNamespace(is_closed=lambda: False,
            evaluate=AsyncMock(side_effect=[True, {'completed': True, 'frames': 24}, True]))
        await source.connect_native_audio(page)
        await source.play()
        await source.stop();await source.stop()
        script, payload = page.evaluate.await_args_list[0].args
        import base64
        self.assertEqual(base64.b64decode(payload['pcm']), stimulus.pcm)
        self.assertIn('node.connect(live.destination)', script)
        self.assertNotIn('node.connect(live.ctx.destination)', script)
        self.assertEqual(page.evaluate.await_count, 3)
        self.assertFalse(hasattr(source, 'push'))
