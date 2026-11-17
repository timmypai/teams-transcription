"""Controlled QA entrypoint and credential-free native receiver observation."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from teams_transcription import qa_probe
from teams_transcription.b_context import MeetingQuestionContext
from teams_transcription.b_response import GREETING


class NativeProbeObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_silent_probe_observes_source_and_render_without_allowing_output(self):
        import asyncio
        from teams_transcription.browser import Browser
        from native_output_fixture import NativeSource
        async with Browser() as browser:
            source = await NativeSource.create(browser)
            sink = qa_probe.NoPhysicalSink()
            try:
                await sink.attach_live(source.page)
                await sink.start()
                before=await sink.rx_snapshot()
                await source.tone()
                async with asyncio.timeout(5):
                    while True:
                        sample = await sink.observe()
                        if sample['energy'] > .001 and sample['source_energy'] > .001:
                            break
                        await asyncio.sleep(.02)
                self.assertTrue(sample['connected'])
                self.assertGreater(sample['packets'], 0)
                self.assertGreater(sample['duration'], 0)
                self.assertGreater(sample['source_frames'], 0)
                self.assertGreater(sample['source_duration'], 0)
                self.assertEqual(sample['source_packets'], sample['packets'])
                after=await sink.rx_snapshot()
                proof=qa_probe.answer_rx({'before':before,'after':after,'complete':True})
                self.assertTrue(proof['verified'],proof)
                self.assertGreater(proof['inbound_delta']['packets_received'],0)
                self.assertGreater(proof['inbound_delta']['bytes_received'],0)
                self.assertGreater(proof['source_delta']['energy'],0)
                with self.assertRaisesRegex(RuntimeError, 'physical_output_forbidden'):
                    await sink.set_muted(False)
                self.assertTrue(await source.page.evaluate(
                    """__live.qaOutput.noPlc===__live.noPlc && !__live.noPlc.track.enabled &&
                       __live.noPlc.ctx.state==='running' && !__live.qaOutput.player"""))
                await source.page.evaluate("__live.noPlc.track.enabled=true")
                with self.assertRaisesRegex(Exception, 'probe_receiver_unavailable'):
                    await sink.observe()
                await sink.pause()
                self.assertTrue((await sink.observe())['connected'])
                await sink.stop()
                await sink.stop()
                self.assertTrue(await source.page.evaluate(
                    "!__live.qaOutput && __live.noPlc.track.readyState==='ended'"))
            finally:
                await sink.stop()
                await source.close()

    async def test_probe_without_prepared_no_plc_receiver_fails_closed(self):
        from teams_transcription.browser import Browser
        from native_output_fixture import NativeSource
        async with Browser() as browser:
            source = await NativeSource.create(browser)
            sink = qa_probe.NoPhysicalSink()
            try:
                await source.page.evaluate("""() => {
                  window.savedNoPlc=__live.noPlc;
                  delete __live.noPlc;
                }""")
                with self.assertRaisesRegex(Exception, 'probe_receiver_unavailable'):
                    await sink.attach_live(source.page)
                self.assertTrue(await source.page.evaluate("!__live.qaOutput"))
            finally:
                await source.page.evaluate("__live.noPlc=window.savedNoPlc")
                await sink.stop()
                await source.close()

    async def test_probe_rejects_replaced_graph_and_keeps_foreign_owner_intact(self):
        from teams_transcription.browser import Browser
        from native_output_fixture import NativeSource
        async with Browser() as browser:
            source = await NativeSource.create(browser)
            sink = qa_probe.NoPhysicalSink()
            try:
                await sink.attach_live(source.page)
                await sink.start()
                await source.page.evaluate("""() => {
                  window.savedNoPlc=__live.noPlc;
                  __live.noPlc={...__live.noPlc};
                }""")
                with self.assertRaisesRegex(Exception, 'probe_receiver_unavailable'):
                    await sink.observe()
                with self.assertRaisesRegex(Exception, 'probe_receiver_unavailable'):
                    await sink.pause()
                await source.page.evaluate("""() => {
                  __live.noPlc=window.savedNoPlc;
                  __live.qaOutput.owner='foreign-owner';
                }""")
                await sink.stop()
                self.assertTrue(await source.page.evaluate(
                    "__live.qaOutput.owner==='foreign-owner' && __live.noPlc.valid()"))
            finally:
                await source.page.evaluate("""owner => {
                  if(window.savedNoPlc) __live.noPlc=window.savedNoPlc;
                  if(__live.qaOutput) __live.qaOutput.owner=owner;
                }""", sink.owner)
                await sink.stop()
                await source.close()


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='qa-private-schema-')
        self.addCleanup(temporary.cleanup)
        self.private_path = Path(temporary.name) / 'private-result.json'

    def args(self, authorized=True):
        return SimpleNamespace(authorize_live=authorized, config='existing-config',
            ice_servers_file=None, chromium=None, private_result=self.private_path)

    async def test_without_authorization_starts_nothing(self):
        with patch.object(qa_probe, 'load_assistant_settings') as settings, \
                patch.object(qa_probe, 'synthetic_pcm') as synth, \
                patch.object(qa_probe, 'Browser') as browser:
            with self.assertRaisesRegex(ValueError, 'explicit_live_authorization_required'):
                await qa_probe.run_probe(self.args(False))
        settings.assert_not_called(); synth.assert_not_called(); browser.assert_not_called()

    async def test_dc_delivery_snapshot_retains_only_bounded_integer_counters(self):
        import json
        native = SimpleNamespace(peer=SimpleNamespace(stats=AsyncMock(return_value={
            'connection':'connected','data_channel':'open','reports':[],
            'datachannel_delivery':{'messages':7,'json_errors':False,
                'binding_attempted':6,'binding_returned':-1,'binding_failed':'PRIVATE-TOKEN',
                'raw_event':'PRIVATE-CONTENT'}})))
        row = await qa_probe.probe_rtp_snapshot(native, 'controlled', qa_probe.time.monotonic())
        self.assertEqual(row['datachannel_delivery'], {'messages':7,'json_errors':None,
            'binding_attempted':6,'binding_returned':None,'binding_failed':None})
        self.assertNotIn('PRIVATE-', json.dumps(row))

    def test_synthesis_allows_only_fixed_synthetic_words(self):
        with patch.object(qa_probe.subprocess, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'fixed_synthetic_input_required'):
                qa_probe.synthetic_pcm('arbitrary input')
        command.assert_not_called()

    def test_mandarin_english_phoneme_fallback_fails_before_wave_synthesis(self):
        def command(args, **kwargs):
            if '-q' in args:
                return SimpleNamespace(stdout='(en)kɪ5ŋθɹˈiː5(cmn)'.encode())
            return SimpleNamespace(stdout=b'\1\0')
        with patch.object(qa_probe.subprocess, 'run', side_effect=command) as run:
            with self.assertRaisesRegex(ValueError, 'synthetic_mandarin_phonemes_invalid'):
                qa_probe.synthetic_pcm(qa_probe.QUESTION)
        self.assertFalse(any('--stdout' in call.args[0] for call in run.call_args_list))

    def test_mandarin_preflight_rejects_bad_hanzi_and_full_sentence_fallback(self):
        for sentinel, sentence in (
                (b'', b'valid'), (b'qing3', b'valid'), (b'\xff', b'valid'),
                (b'x'*4097, b'valid'),
                ('tɕhˈi2ŋ'.encode(), b''),
                ('tɕhˈi2ŋ'.encode(), 'tɕhˈi2ŋ (en)kɪ5ŋ(cmn)'.encode()),
                ('tɕhˈi2ŋ'.encode(), 'tɕhˈi2ŋ (en-us)wrong(cmn)'.encode())):
            with self.subTest(sentinel=sentinel[:30], sentence=sentence):
                with patch.object(qa_probe.subprocess, 'run', side_effect=[
                        SimpleNamespace(stdout=sentinel), SimpleNamespace(stdout=sentence)]) as run:
                    with self.assertRaisesRegex(ValueError, 'synthetic_mandarin_phonemes_invalid'):
                        qa_probe.synthetic_pcm(qa_probe.QUESTION)
                self.assertFalse(any('--stdout' in call.args[0] for call in run.call_args_list))

    async def test_invalid_phonemes_stop_before_browser_model_and_keep_safe_receipt(self):
        import json
        args = self.args(); args.qa_start_stage = 'question'
        with patch.object(qa_probe, 'load_assistant_settings', return_value=SimpleNamespace()), \
                patch.object(qa_probe.subprocess, 'run', return_value=SimpleNamespace(stdout=b'PRIVATE-PHONEME')), \
                patch.object(qa_probe, 'Browser') as browser, patch.object(qa_probe, 'NativeLiveResponder') as native:
            result = await qa_probe.run_probe(args)
        self.assertFalse(result['accepted'])
        self.assertEqual(result['failure']['phase'], 'synthetic_input')
        browser.assert_not_called(); native.assert_not_called()
        self.assertNotIn('PRIVATE-PHONEME', json.dumps(result))
        self.assertNotIn('PRIVATE-PHONEME', self.private_path.read_text())

    def test_fixed_question_requests_chinese_source_quote_with_mandarin_voice(self):
        self.assertEqual(qa_probe.QUESTION, '請只用繁體中文逐字引用會議預算原文，不加說明。')
        with patch.object(qa_probe.subprocess, 'run', side_effect=[
                SimpleNamespace(stdout='tɕhˈi2ŋ'.encode()), SimpleNamespace(stdout='tɕhˈi2ŋ ʈʂɨ'.encode()),
                SimpleNamespace(stdout=b'controlled-wave'), SimpleNamespace(stdout=b'\1\0')]) as command:
            self.assertEqual(qa_probe.synthetic_pcm(qa_probe.QUESTION), b'\1\0')
        self.assertEqual(command.call_args_list[0].args[0],
            ['espeak-ng', '-q', '--ipa', '-v', 'cmn-latn-pinyin', '請'])
        self.assertEqual(command.call_args_list[1].args[0],
            ['espeak-ng', '-q', '--ipa', '-v', 'cmn-latn-pinyin', qa_probe.QUESTION])
        self.assertEqual(command.call_args_list[2].args[0],
            ['espeak-ng', '--stdout', '-v', 'cmn-latn-pinyin', qa_probe.QUESTION])

    def test_fixed_phonetic_wake_retains_english_voice(self):
        with patch.object(qa_probe.subprocess, 'run', side_effect=[
                SimpleNamespace(stdout=b'controlled-wave'), SimpleNamespace(stdout=b'\1\0')]) as command:
            self.assertEqual(qa_probe.synthetic_pcm(qa_probe.WAKE), b'\1\0')
        self.assertEqual(command.call_args_list[0].args[0],
            ['espeak-ng', '--stdout', '-v', 'en', 'xiaobai xiaobai'])

    async def test_missing_synthesizer_fails_before_browser_or_model_and_keeps_safe_receipt(self):
        import json
        with patch.object(qa_probe,'load_assistant_settings',return_value=SimpleNamespace()), \
                patch.object(qa_probe,'synthetic_pcm',side_effect=FileNotFoundError('PRIVATE-EXCEPTION')), \
                patch.object(qa_probe,'Browser') as browser,patch.object(qa_probe,'NativeLiveResponder') as native:
            result=await qa_probe.run_probe(self.args())
        self.assertFalse(result['accepted'])
        self.assertEqual(result['failure']['phase'],'synthetic_input')
        browser.assert_not_called();native.assert_not_called()
        self.assertNotIn('PRIVATE-EXCEPTION',json.dumps(result))
        self.assertNotIn('PRIVATE-EXCEPTION',self.private_path.read_text())

    async def test_pcm_enters_admitted_input_with_chunk_timestamps_and_pacing(self):
        pcm = b'\x01\x00' * 1100
        native = SimpleNamespace(meeting_id='synthetic-qa', error=None,
            push_rx=Mock(return_value=True), peer=SimpleNamespace(push=AsyncMock()))
        with patch.object(qa_probe.time, 'monotonic', side_effect=[100, 101]), \
                patch.object(qa_probe.asyncio, 'sleep', new_callable=AsyncMock) as sleep:
            await qa_probe.send_pcm(native, pcm)
        self.assertEqual(native.push_rx.call_count, 2)
        for call, chunk, ended in zip(native.push_rx.call_args_list,
                (pcm[:2048], pcm[2048:]), (100, 101)):
            self.assertEqual(call.args, ('synthetic-qa', chunk))
            self.assertEqual(call.kwargs,
                {'started_at':ended-len(chunk)/48000, 'ended_at':ended})
        self.assertEqual([call.args[0] for call in sleep.await_args_list],
            [2048/48000, (len(pcm)-2048)/48000])
        native.peer.push.assert_not_awaited()

    async def test_rejected_clip_stops_without_sending_remaining_chunks(self):
        native = SimpleNamespace(meeting_id='synthetic-qa', error=None,
            push_rx=Mock(return_value=False))
        with self.assertRaisesRegex(RuntimeError, 'qa_pcm_rejected'):
            await qa_probe.send_pcm(native, b'\x01\x00' * 2200)
        self.assertEqual(native.push_rx.call_count, 1)

    async def test_wait_supplies_observed_synthetic_quiet_and_rejects_failed_admission(self):
        for accepted in (True, False):
            with self.subTest(accepted=accepted):
                native = SimpleNamespace(meeting_id='synthetic-qa', error=None,
                    state='listening', push_rx=Mock(return_value=accepted),
                    intervals=SimpleNamespace(overlaps=Mock(return_value=False)))
                with patch.object(qa_probe.time, 'monotonic', return_value=100), \
                        patch.object(qa_probe.asyncio, 'sleep', new_callable=AsyncMock):
                    if accepted:
                        await qa_probe.wait_until(native, lambda: native.push_rx.called)
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'qa_pcm_rejected'):
                            await qa_probe.wait_until(native, lambda: False)
                native.push_rx.assert_called_once_with('synthetic-qa', b'\0' * 960,
                    started_at=99.98, ended_at=100)

    async def test_quiet_is_not_injected_during_owned_output_or_overlapping_tail(self):
        for state, overlaps in (('answering', False), ('listening', True)):
            with self.subTest(state=state, overlaps=overlaps):
                native = SimpleNamespace(meeting_id='synthetic-qa', error=None,
                    state=state, push_rx=Mock(),
                    intervals=SimpleNamespace(overlaps=Mock(return_value=overlaps)))
                condition = Mock(side_effect=[False, True])
                with patch.object(qa_probe.asyncio, 'sleep', new_callable=AsyncMock):
                    await qa_probe.wait_until(native, condition)
                native.push_rx.assert_not_called()


    async def mock_success_probe(self, *, text_gate=False, media_profile="legacy", media_case='valid', answers=None,
            extra_utterance=None, context_trace=None, lifecycle_trace=None):
        from teams_transcription.b_question import ApplicationQuestionFramer
        class Native:
            def __init__(self, browser, runtime, ident, output, tool, **kwargs):
                self.output, self.tool = output, tool
                self.context = MeetingQuestionContext(tool)
                self.context.bind_session('mock-thread')
                self.state, self.error, self.tool_calls = 'sleeping', None, 0
                self.stop_status = 'not_started'
                self.stop_ack_received = self.stop_closed_received = False
                self.cleanup_status = 'idle'
                self.muted_probe_text_gate = text_gate
                self.greeting_completion = None
                self.wake_words = ('小白小白','xiaobai xiaobai')
                self.question_framer = ApplicationQuestionFramer(self.wake_words)
                assert kwargs['peer_media_profile'] == media_profile
                self.peer = SimpleNamespace(stats=AsyncMock(return_value={
                    'connection':'connected','data_channel':'open','sent_frames':100,
                    'audio_context_state':'running','reports':[{'bytesSent':40,'packetsSent':2}]}))
            async def start(self, *, enable_output):
                assert enable_output is False
                await self.output.attach_live(object())
            def status(self):
                if context_trace is not None:
                    assert self.stop_status == 'stopped', 'diagnostics must follow owned cleanup'
                return {'tool_deliveries':context_trace or []}
            def turn_lifecycle_snapshot(self):
                if lifecycle_trace == 'unavailable':
                    raise RuntimeError('PRIVATE-TRACE-ERROR')
                return lifecycle_trace
            async def handle_notification(self, method, params):
                if params.get('role') == 'user':
                    if method.endswith('/delta'):
                        self.question_framer.append_transcript(params['delta'])
                        if self.state == 'sleeping' and self.question_framer.pending_status()['has_wake']:
                            self.state = 'greeting'
            async def stop(self):
                self.stop_status = 'stopped'
                self.stop_ack_received = media_case != 'missing_stop_ack'
                self.stop_closed_received = media_case != 'missing_closed'
                self.cleanup_status = 'unknown' if media_case == 'unknown_native_cleanup' else 'cleaned'
                return 'stopped'
        async def speech(native, pcm):
            if native.state == 'sleeping':
                native.question_framer.reset()
                ended_at = qa_probe.time.monotonic()
                native.question_framer.push_pcm(b'\x00\x20' * 480,
                    started_at=ended_at - .02, ended_at=ended_at)
                for chunk in ('xiaobai ', 'xiaobai'):
                    await native.handle_notification('thread/realtime/transcript/delta',
                        {'role':'user', 'delta':chunk})
                native.state = 'listening'; text = GREETING
                if text_gate:
                    native.greeting_completion = {'gate':'synthetic_muted_text_only',
                        'text_complete':True, 'provider_done':False,
                        'audio_completion':'unverified', 'local_lease_closed':True}
                    await native.handle_notification('thread/realtime/transcript/delta',
                        {'role':'assistant', 'delta':GREETING})
                    return
            else:
                await native.handle_notification('thread/realtime/transcript/done',
                    {'role':'user', 'text':qa_probe.QUESTION})
                plan = await native.context.prepare(qa_probe.QUESTION, session_id='mock-thread')
                native.context.record_sent(plan, len(plan.payload.encode()) + 15,
                    session_id='mock-thread')
                native.context.commit(plan, session_id='mock-thread')
                text = next(reading['text'] for reading in plan.result['selected_source_reading']
                            if '預算' in reading['text'])
                if answers is not None:
                    text = answers[native.tool_calls]
                native.tool_calls += 1; native.state = 'answering'
                if media_case != 'text_only':await native.output.begin_speech('synthetic-qa')
                if text_gate:
                    # Preserve and isolate a late canonical greeting final.
                    await native.handle_notification('thread/realtime/transcript/done',
                        {'role':'assistant', 'text':GREETING})
            if text != GREETING:
                if extra_utterance is not None:
                    await native.handle_notification('thread/realtime/transcript/done',
                        {'role':'assistant', 'text':extra_utterance})
                await native.handle_notification('thread/realtime/transcript/delta',
                    {'role':'assistant', 'delta':text[:3]})
                await native.handle_notification('thread/realtime/transcript/delta',
                    {'role':'assistant', 'delta':text[3:]})
            await native.handle_notification('thread/realtime/transcript/done',
                {'role': 'assistant', 'text': text})
            if text != GREETING:
                if media_case != 'text_only':await native.output.end_speech(quiet=True)
                native.state='sleeping'
        browser = AsyncMock()
        browser.browser = object()
        browser.playwright = browser._starter = None
        guard = SimpleNamespace(prepare=AsyncMock(), adapter=Mock(return_value=object()))
        samples=0
        async def rx_sample():
            nonlocal samples
            samples+=1
            count=samples
            if media_case=='previous_round_only':count=min(count,2)
            if media_case=='counter_reset' and count%2==0:count=0
            inbound=0 if media_case=='tx_only' else count*100
            source=count if media_case not in {'plc_only','tx_only'} else 0
            value={'connected':True,'inbound':{'ssrc':123,'packets_received':inbound,'bytes_received':inbound*50},
                'source':{'packets':source*80,'frames':source*4800,'duration':source*.1,
                    'energy':0 if media_case=='comfort_noise' else source*.001},
                'render':{'frames':samples*4800,'duration':samples*.1,'energy':samples*.001}}
            if media_case=='receiver_changed' and samples%2==0:value['inbound']['ssrc']=456
            if media_case=='invalid_counter':value['source']['energy']=float('nan')
            if media_case=='tiny_duration':value['source']['duration']=samples*5e-324
            if media_case=='below_activity':value['source']['energy']=source*1e-12
            if media_case=='private_counter':value['inbound']['bytes_received']='PRIVATE-TOKEN-SDP'
            if media_case=='missing_ssrc':value['inbound']['ssrc']=None
            if media_case=='first_packet' and samples==1:
                value['inbound']={'ssrc':None,'packets_received':0,'bytes_received':0}
                value['source']={'packets':0,'frames':0,'duration':0,'energy':0}
            return value
        sink=SimpleNamespace(attach_live=AsyncMock(),start=AsyncMock(),set_muted=AsyncMock(),
            pause=AsyncMock(return_value={'status':'muted','truncated':False}),
            observe=AsyncMock(return_value={'connected':True,'frames':0,'packets':0,'energy':0,'duration':0,
                'source_packets':0,'source_frames':0,'source_energy':0,'source_duration':0}),
            rx_snapshot=rx_sample)
        async def inline_io(function, *args, **kwargs):
            # Synthetic/model I/O are mocks; source persistence still real.
            return function(*args, **kwargs)
        wait = qa_probe.wait_until
        async def bounded_wait(native, condition):
            return await wait(native, condition, timeout=.2)
        with patch('asyncio.to_thread', side_effect=inline_io), \
                patch.object(qa_probe, 'wait_until', side_effect=bounded_wait), \
                patch.object(qa_probe, 'load_assistant_settings', return_value=SimpleNamespace(
                allowed_egress_hosts=[], codex_bin='never-called')), \
                patch.object(qa_probe, 'synthetic_pcm', return_value=b'\1\0'), \
                patch.object(qa_probe, 'Browser', return_value=browser), \
                patch.object(qa_probe, 'RuntimeLiveGuard', return_value=guard), \
                patch.object(qa_probe, 'NoPhysicalSink', return_value=sink), \
                patch.object(qa_probe, 'NativeLiveResponder', Native), \
                patch.object(qa_probe, 'send_pcm', side_effect=speech):
            args = self.args(); args.peer_media_profile = media_profile
            result = await qa_probe.run_probe(args)
        if answers is not None or extra_utterance is not None:
            return result
        if media_case not in {'valid','first_packet'}:
            self.assertFalse(result['accepted'], 'text/TX without actual same-answer RX must fail')
            return result
        self.assertTrue(result['accepted'])  # Injected mock result only.
        self.assertEqual(len(result['checks']), 2)
        self.assertTrue(all(check['exact_source_quote'] for check in result['checks']))
        self.assertEqual(result['media_diagnostics']['profile'], media_profile)
        self.assertEqual(result['media_diagnostics']['user_transcript_events'], 6)
        self.assertEqual(result['media_diagnostics']['wake_matches'], 2)
        self.assertEqual(result['media_diagnostics']['greeting_entries'], 2)
        self.assertEqual([r['point'] for r in result['media_diagnostics']['rtp_snapshots']],
            ['wake_before_send_1','wake_after_send_1','wake_before_send_2','wake_after_send_2'])
        self.assertFalse(result['teams_verified'])
        self.assertFalse(result['physical_tx_verified'])
        self.assertFalse(result['context']['model_consumption_verified'])
        import hashlib
        import json
        import stat
        raw = self.private_path.read_bytes()
        private = json.loads(raw)
        self.assertEqual(stat.S_IMODE(self.private_path.stat().st_mode), 0o600)
        self.assertEqual(result['private_evidence']['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(private['schema_version'], 1)
        self.assertEqual(private['kind'], 'private_synthetic_qa_evidence')
        self.assertEqual(len(private['rounds']), 2)
        self.assertEqual(len(private['context_plans']), 2)
        self.assertFalse(private['context_plans_truncated'])
        self.assertEqual(private['control_receipt']['context'], result['context'])
        self.assertEqual(private['control_receipt']['owned_cleanup'], result['owned_cleanup'])
        for revision, row in enumerate(private['rounds'], 1):
            paragraph = '預算十萬元。' if revision == 1 else '預算十二萬元。'
            context_plan = private['context_plans'][revision-1]
            self.assertTrue(context_plan['constructed_only'])
            self.assertFalse(context_plan['model_consumption_verified'])
            self.assertRegex(context_plan['payload_sha256'], r'^[0-9a-f]{64}$')
            self.assertRegex(context_plan['snapshot_sha256'], r'^[0-9a-f]{64}$')
            self.assertEqual(context_plan['readings'], [{'text_sha256':hashlib.sha256(paragraph.encode()).hexdigest(),
                'revision':revision}])
            self.assertEqual(row['source_event_revision'], revision)
            self.assertEqual(row['question'], '請只用繁體中文逐字引用會議預算原文，不加說明。')
            self.assertEqual(''.join(e['text'] for e in row['user_transcripts'] if e['kind']=='delta'), 'xiaobai xiaobai')
            self.assertTrue(row['user_transcripts'][1]['wake_match'])
            self.assertTrue(row['user_transcripts'][1]['greeting_entered'])
            self.assertEqual(row['user_transcripts'][1]['state_after'], 'greeting')
            self.assertEqual(row['source_paragraph'], paragraph)
            self.assertEqual(row['source_paragraph_sha256'], hashlib.sha256(paragraph.encode()).hexdigest())
            self.assertIn(paragraph, row['answer_text'])  # Exact raw answer remains inspectable.
            self.assertEqual(row['assistant_utterances'][0]['text'], GREETING)
            self.assertEqual(row['assistant_utterances'][-1]['text'], row['answer_text'])
            self.assertEqual(''.join(d['text'] for d in row['assistant_deltas'] if d['phase'].startswith('question_')), row['answer_text'])
            timing = row['timing']
            self.assertIsNotNone(timing['question_send_begin_seconds'])
            self.assertGreaterEqual(timing['question_send_complete_seconds'], timing['question_send_begin_seconds'])
            self.assertIsNotNone(timing['first_useful_text_seconds'])
            self.assertEqual(row['first_useful_text_method'], 'thread/realtime/transcript/delta')
            self.assertAlmostEqual(timing['first_useful_text_from_question_send_begin_seconds'],
                timing['first_useful_text_seconds'] - timing['question_send_begin_seconds'])
            # Mock returns text during send; signed values must not be clamped/fabricated.
            self.assertLessEqual(timing['first_useful_text_from_question_send_complete_seconds'], 0)
            for key in ('question_end_seconds', 'first_nonzero_audio_seconds',
                        'first_useful_audio_seconds', 'first_useful_text_from_question_end_seconds',
                        'first_nonzero_audio_from_question_send_begin_seconds',
                        'first_nonzero_audio_from_question_send_complete_seconds',
                        'first_nonzero_audio_from_question_end_seconds'):
                self.assertIsNone(timing[key])
        control_json = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(GREETING, control_json)
        self.assertNotIn('預算十萬元', control_json)
        self.assertNotIn('預算十二萬元', control_json)
        self.assertNotIn('assistant_utterances', control_json)
        return result

    async def test_mock_only_probe_checks_greeting_tool_revision_muted_and_stop(self):
        await self.mock_success_probe()

    async def test_optional_lifecycle_evidence_cannot_accept_missing_audio_or_reject_valid_probe(self):
        import json
        result = await self.mock_success_probe(media_case='text_only',
            lifecycle_trace={'current':{'done_is_set':True},'counts':{'dc_turn_done':99}})
        self.assertFalse(result['accepted'])
        self.assertTrue(result['turn_lifecycle']['current']['done_is_set'])
        self.private_path.unlink()
        result = await self.mock_success_probe(lifecycle_trace='unavailable')
        self.assertTrue(result['accepted'])
        self.assertIsNone(result['turn_lifecycle'])
        self.assertNotIn('PRIVATE-TRACE-ERROR', json.dumps(result))
        self.assertNotIn('PRIVATE-TRACE-ERROR', self.private_path.read_text())

    async def test_private_context_metadata_keeps_only_typed_receipt_fields(self):
        import json
        await self.mock_success_probe(context_trace=[{'phase':['PRIVATE-CONTEXT'],
            'snapshot_sha256':'PRIVATE-CONTEXT', 'payload_sha256':'b'*64,
            'source_version':True, 'payload_bytes':'PRIVATE-CONTEXT',
            'sent_bytes':42, 'marker_returned':True, 'raw_context':'PRIVATE-CONTEXT'}])
        private = json.loads(self.private_path.read_text())
        receipt = private['context_deliveries'][0]
        self.assertEqual(receipt['phase'], 'unavailable')
        self.assertIsNone(receipt['snapshot_sha256'])
        self.assertIsNone(receipt['source_version'])
        self.assertIsNone(receipt['payload_bytes'])
        self.assertEqual(receipt['payload_sha256'], 'b'*64)
        self.assertEqual(receipt['sent_bytes'], 42)
        self.assertTrue(receipt['marker_returned'])
        self.assertNotIn('PRIVATE-CONTEXT', self.private_path.read_text())

    async def test_correct_budget_with_neutral_prefix_passes_versioned_semantic_criterion(self):
        result = await self.mock_success_probe(answers=('答案是：預算十萬元。', '預算十二萬元。'))
        self.assertTrue(all(row['audio_rx_verified'] for row in result['checks']))
        self.assertTrue(result['owned_cleanup_verified'])
        self.assertFalse(result['checks'][0]['exact_source_quote'])
        self.assertEqual(result['content_criterion_version'], 'synthetic_current_fact_semantic_v1')
        self.assertTrue(result['accepted'])

    async def test_audio_and_budget_terms_do_not_accept_translation_wrong_value_or_extra_text(self):
        cases = (
            ('The budget is one hundred thousand.', 'The budget is one hundred twenty thousand.'),
            ('預算十萬元，也可能九萬元。', '預算十二萬元。'),
            ('預算十萬元。', '預算十二萬元，也可能十萬元。'),
            ('預算十萬元。', '預算十萬元。'),
            ('預算九萬元。', '預算十二萬元。'),
            ('預算十萬元。額外說明。', '預算十二萬元。'),
            ('預算十萬元。', '預算十二萬元。額外說明。'),
        )
        for answers in cases:
            with self.subTest(answers=answers):
                self.private_path.unlink(missing_ok=True)
                result = await self.mock_success_probe(answers=answers)
                self.assertTrue(all(row['audio_rx_verified'] for row in result['checks']))
                self.assertTrue(result['owned_cleanup_verified'])
                self.assertTrue(any(not row['exact_source_quote'] for row in result['checks']))
                self.assertFalse(result['accepted'])

    async def test_exact_current_source_quotes_allow_only_outer_whitespace(self):
        result = await self.mock_success_probe(answers=(' \n預算十萬元。\n', '\t預算十二萬元。 '))
        self.assertTrue(all(row['exact_source_quote'] for row in result['checks']))
        self.assertTrue(result['accepted'])

    async def test_correct_last_utterance_cannot_hide_earlier_prefix_wrong_value_or_duplicate(self):
        for extra in ('額外說明：答案如下。', '預算九萬元。', '預算十萬元。'):
            with self.subTest(extra=extra):
                self.private_path.unlink(missing_ok=True)
                result = await self.mock_success_probe(extra_utterance=extra)
                self.assertTrue(all(row['audio_rx_verified'] for row in result['checks']))
                self.assertTrue(result['owned_cleanup_verified'])
                self.assertTrue(all(not row['exact_source_quote'] for row in result['checks']))
                self.assertFalse(result['accepted'])

    async def test_text_and_outbound_packets_alone_cannot_pass_audio_rx(self):
        await self.mock_success_probe(media_case='text_only')

    async def test_first_answer_can_establish_its_first_inbound_ssrc_from_zero(self):
        await self.mock_success_probe(media_case='first_packet')

    async def test_tx_plc_or_comfort_noise_cannot_replace_decoded_source_audio(self):
        for case in ('tx_only','plc_only','comfort_noise'):
            with self.subTest(case=case):
                self.private_path.unlink(missing_ok=True)
                await self.mock_success_probe(media_case=case)

    async def test_stale_receiver_reset_or_private_counter_never_passes(self):
        import json
        for case in ('previous_round_only','counter_reset','receiver_changed','invalid_counter','private_counter',
                'tiny_duration','below_activity','missing_ssrc'):
            with self.subTest(case=case):
                self.private_path.unlink(missing_ok=True)
                result=await self.mock_success_probe(media_case=case)
                self.assertNotIn('PRIVATE-TOKEN-SDP',json.dumps(result))
                self.assertNotIn('PRIVATE-TOKEN-SDP',self.private_path.read_text())

    async def test_audio_without_confirmed_protocol_cleanup_is_not_accepted(self):
        for case in ('missing_stop_ack','missing_closed','unknown_native_cleanup'):
            with self.subTest(case=case):
                self.private_path.unlink(missing_ok=True)
                result=await self.mock_success_probe(media_case=case)
                self.assertTrue(all(row['audio_rx_verified'] for row in result['checks']))
                self.assertFalse(result['owned_cleanup_verified'])

    async def test_probe_receipt_marks_delta_gate_unverified_and_isolates_late_done(self):
        await self.mock_success_probe(text_gate=True)
        import json
        private = json.loads(self.private_path.read_text())
        for row, check in zip(private['rounds'], private['control_receipt']['checks']):
            self.assertTrue(check['exact_greeting'])
            self.assertTrue(check['greeting_completion']['text_complete'])
            self.assertFalse(check['greeting_completion']['provider_done'])
            self.assertEqual(check['greeting_completion']['audio_completion'], 'unverified')
            self.assertEqual(row['greeting_completion'], check['greeting_completion'])
            self.assertTrue(row['assistant_utterances'][0]['isolated_late_greeting'])
            self.assertNotEqual(row['answer_text'], GREETING)

    async def failure_probe(self, *, mode):
        import asyncio
        import json
        instances = []
        class Native:
            def __init__(self, browser, runtime, ident, output, tool, **kwargs):
                self.output, self.tool = output, tool
                self.meeting_id = ident
                self.context = MeetingQuestionContext(tool)
                self.context.bind_session('mock-thread')
                self.state, self.error, self.tool_calls = 'sleeping', None, 0
                self.stop_status = 'not_started'
                self.stop_ack_received = self.stop_closed_received = False
                self.cleanup_status = 'idle'
                self.rpc = SimpleNamespace(process=None, tasks=[], _close_task=None)
                instances.append(self)
            async def start(self, **kwargs):
                self.rpc.process = await asyncio.create_subprocess_exec('sleep', '30')
                if mode == 'start_timeout':
                    raise TimeoutError('injected initialization timeout')
                if mode == 'start_failure':
                    plan = await self.context.prepare('預算', session_id='mock-thread')
                    self.context.record_sent(plan, 50, session_id='mock-thread')
                    raise RuntimeError('private-token SDP meeting URL must not be printed')
            async def handle_notification(self, *args): pass
            def push_rx(self, meeting_id, pcm, *, started_at, ended_at):
                return meeting_id == self.meeting_id
            async def stop(self):
                if mode == 'spawn_rpc_on_cancel':
                    async def closing():
                        try: await asyncio.Event().wait()
                        finally: self.rpc._close_task = asyncio.create_task(asyncio.Event().wait())
                    self._close_task = asyncio.create_task(closing())
                    await asyncio.shield(self._close_task)
                if mode == 'hang_close': await asyncio.Event().wait()
                self.context.invalidate('mock-thread')
                self.stop_status = 'unknown'
                return 'unknown'
        async def speech(native, pcm):
            if mode == 'wake_timeout':
                native.state = 'greeting'
                return
            if mode == 'wrong_answer':
                if native.state == 'sleeping':
                    native.state = 'listening'; text = GREETING
                else:
                    native.tool_calls += 1; native.state = 'sleeping'
                    text = '原樣保留：預算九萬元，並無來源支持。'
                await native.handle_notification('thread/realtime/transcript/done',
                    {'role':'assistant', 'text':text})
            elif native.state == 'sleeping': native.state = 'listening'
            else:
                if mode == 'work_timeout':
                    await native.handle_notification('thread/realtime/transcript/delta',
                        {'role':'assistant', 'delta':'依目前記錄'})
                await asyncio.Event().wait()
        async def inline_io(function, *args, **kwargs): return function(*args, **kwargs)
        browser = AsyncMock(); browser.browser = object()
        browser.playwright = browser._starter = None
        guard = SimpleNamespace(prepare=AsyncMock(), adapter=Mock(return_value=object()))
        wait = qa_probe.wait_until
        async def bounded_wait(native, condition):
            return await wait(native, condition, timeout=.01 if mode == 'wake_timeout' else 45)
        try:
            with patch('asyncio.to_thread', side_effect=inline_io), \
                    patch.object(qa_probe, 'wait_until', side_effect=bounded_wait), \
                    patch.object(qa_probe, 'WORK_SECONDS', 1 if mode in {'start_failure','wake_timeout','start_timeout'} else .03), \
                    patch.object(qa_probe, 'CLEANUP_SECONDS', .15), \
                    patch.object(qa_probe, 'load_assistant_settings', return_value=SimpleNamespace(
                        allowed_egress_hosts=['existing.test'], codex_bin='never-called')), \
                    patch.object(qa_probe, 'synthetic_pcm', return_value=b'\1\0'), \
                    patch.object(qa_probe, 'Browser', return_value=browser), \
                    patch.object(qa_probe, 'RuntimeLiveGuard', return_value=guard), \
                    patch.object(qa_probe, 'NativeLiveResponder', Native), \
                    patch.object(qa_probe, 'send_pcm', side_effect=speech):
                result = await qa_probe.run_probe(self.args())
            self.assertIsNotNone(instances[0].rpc.process.returncode)  # Before fallback cleanup.
            if instances[0].rpc._close_task:
                self.assertTrue(instances[0].rpc._close_task.done())  # Before fallback.
            self.assertTrue(result['owned_cleanup']['reaped'], str(result['owned_cleanup']))
            self.assertEqual(result['owned_cleanup']['live_processes'], 0)
            self.assertFalse(result['accepted'])
            self.assertFalse(result['teams_verified'])
            self.assertNotIn('private-token', json.dumps(result))
            return result
        finally:
            for native in instances:
                for task in (getattr(native, '_close_task', None), native.rpc._close_task):
                    if task and not task.done():
                        task.cancel(); await asyncio.gather(task, return_exceptions=True)
                process = native.rpc.process
                if process and process.returncode is None:
                    process.kill(); await process.wait()

    async def test_start_failure_keeps_phase_partial_cursor_and_owned_stop_receipt(self):
        result = await self.failure_probe(mode='start_failure')
        self.assertEqual(result['failure']['phase'], 'qa_start')
        self.assertEqual(result['media_diagnostics']['rtp_snapshots'][-1]['point'], 'failure_before_stop')
        self.assertEqual(result['context_before_stop']['sent_bytes'], 50)
        self.assertTrue(result['context_before_stop']['pending'])
        self.assertFalse(result['context']['pending'])
        self.assertEqual(result['stop_status'], 'unknown')
        self.assertFalse(result['owned_cleanup']['stop_closed_received'])
        self.assertEqual(result['owned_cleanup']['status'], 'unknown')

    async def test_whole_probe_deadline_returns_stage_receipt_and_reaps_child(self):
        result = await self.failure_probe(mode='work_timeout')
        self.assertEqual(result['failure']['error_code'], 'work_deadline_exceeded')
        self.assertEqual(result['failure']['phase'], 'question_revision_1')
        self.assertEqual(result['failure']['timeout_layer'], 'whole_work')
        self.assertEqual(result['failure']['timeout_seconds'], .03)
        self.assertEqual(result['context_before_stop']['sent_bytes'], 0)
        import json
        private = json.loads(self.private_path.read_text())
        timing = private['rounds'][0]['timing']
        self.assertIsNotNone(timing['question_send_begin_seconds'])
        self.assertIsNone(timing['question_send_complete_seconds'])
        self.assertIsNone(timing['question_end_seconds'])
        self.assertIsNone(timing['first_useful_text_seconds'])
        self.assertEqual(private['control_receipt']['failure'], result['failure'])
        self.assertIsNone(private['rounds'][0]['answer_text'])
        self.assertEqual(private['rounds'][0]['assistant_deltas'][0]['text'], '依目前記錄')

    async def test_hung_close_is_bounded_and_retains_unknown_cleanup(self):
        result = await self.failure_probe(mode='hang_close')
        self.assertEqual(result['failure']['error_code'], 'work_deadline_exceeded')
        self.assertEqual(result['owned_cleanup']['status'], 'unknown')
        self.assertEqual(result['stop_status'], 'unknown')

    async def test_cancellation_finally_spawned_rpc_close_is_counted_and_closed(self):
        result = await self.failure_probe(mode='spawn_rpc_on_cancel')
        self.assertEqual(result['owned_cleanup']['pending_close_tasks'], 0)
        self.assertEqual(result['owned_cleanup']['status'], 'unknown')

    async def test_cleanup_deadline_retains_observed_owned_resource_state(self):
        await self.native_start_cleanup_probe(hold_cleanup_receipt=True)

    async def test_cleanup_caller_refreshes_completed_operation_without_erasing_window_snapshot(self):
        import json
        result, snapshot, operation = await self.cleanup_snapshot_handoff_probe(release_before_return=True)
        self.assertTrue(operation.done())
        self.assertTrue(snapshot['pending_operation'])
        self.assertFalse(result['owned_cleanup']['pending_operation'])
        self.assertEqual(result['owned_cleanup']['window_snapshot'], snapshot)
        self.assertEqual(result['owned_cleanup']['status'], 'unknown')
        self.assertFalse(result['owned_cleanup_verified'])
        self.assertFalse(result['accepted'])
        private = json.loads(self.private_path.read_text())
        self.assertEqual(private['control_receipt']['owned_cleanup'], result['owned_cleanup'])

    async def test_cleanup_caller_does_not_claim_unfinished_operation_has_completed(self):
        import json
        result, snapshot, operation = await self.cleanup_snapshot_handoff_probe(release_before_return=False)
        self.assertTrue(snapshot['pending_operation'])
        self.assertTrue(result['owned_cleanup']['pending_operation'])
        self.assertEqual(result['owned_cleanup']['window_snapshot'], snapshot)
        self.assertEqual(result['owned_cleanup']['status'], 'unknown')
        self.assertFalse(result['owned_cleanup_verified'])
        self.assertFalse(result['accepted'])
        # The fixture subsequently releases its task during teardown. A receipt
        # is not silently rewritten into live state, including its private copy.
        self.assertTrue(operation.done())
        private = json.loads(self.private_path.read_text())
        self.assertEqual(private['control_receipt']['owned_cleanup'], result['owned_cleanup'])

    async def cleanup_snapshot_handoff_probe(self, *, release_before_return):
        """Replay one isolated caller handoff, with the real cleanup algorithm.

        The explicit expired deadline forces the window boundary, not a CPU/load
        guess. No previous test, extra wall sleep, native channel or subprocess
        is required. The outer work/cleanup budgets retain the existing fixture
        values. This does not reconstruct the historical aggregate's timeline.
        """
        import asyncio
        entered, release = asyncio.Event(), asyncio.Event()
        receipt = {}
        operation = None

        class Native:
            def __init__(self, browser, runtime, ident, output, tool, **kwargs):
                self.output = output
                self.context = MeetingQuestionContext(tool)
                self.state, self.error = 'starting', None
                self.rpc = SimpleNamespace(process=None, tasks=[])

            async def start(self, **kwargs):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    # A shielded owner can remain pending through more than one
                    # cancellation until its already-owned cleanup is released.
                    while not release.is_set():
                        try:
                            await release.wait()
                        except asyncio.CancelledError:
                            pass
                    raise

            async def handle_notification(self, *args):
                pass

            async def stop(self):
                return 'unknown'

        original_cleanup = qa_probe.cleanup_probe

        async def handoff(native, browser, owned, **kwargs):
            nonlocal operation
            operation = kwargs['operation_task']
            self.assertTrue(entered.is_set())
            kwargs['deadline'] = asyncio.get_running_loop().time()
            value = await original_cleanup(native, browser, owned, **kwargs)
            receipt.update(value)
            self.assertTrue(value['pending_operation'])
            if release_before_return:
                release.set()
                await asyncio.gather(operation, return_exceptions=True)
            return value

        async def inline_io(function, *args, **kwargs):
            return function(*args, **kwargs)

        browser = AsyncMock()
        browser.browser = object()
        browser.playwright = browser._starter = None
        guard = SimpleNamespace(prepare=AsyncMock(), adapter=Mock(return_value=object()))
        try:
            with patch('asyncio.to_thread', side_effect=inline_io), \
                    patch.object(qa_probe, 'WORK_SECONDS', .03), \
                    patch.object(qa_probe, 'CLEANUP_SECONDS', .15), \
                    patch.object(qa_probe, 'load_assistant_settings', return_value=SimpleNamespace(
                        allowed_egress_hosts=['existing.test'], codex_bin='never-called')), \
                    patch.object(qa_probe, 'synthetic_pcm', return_value=b'\1\0'), \
                    patch.object(qa_probe, 'Browser', return_value=browser), \
                    patch.object(qa_probe, 'RuntimeLiveGuard', return_value=guard), \
                    patch.object(qa_probe, 'NativeLiveResponder', Native), \
                    patch.object(qa_probe, 'cleanup_probe', side_effect=handoff):
                result = await asyncio.wait_for(qa_probe.run_probe(self.args()), 1)
            self.assertEqual(result['failure']['error_code'], 'work_deadline_exceeded')
            self.assertEqual(result['failure']['phase'], 'qa_start')
            if not release_before_return:
                self.assertFalse(operation.done())
            return result, dict(receipt), operation
        finally:
            release.set()
            if operation is not None:
                await asyncio.gather(operation, return_exceptions=True)

    async def test_real_native_start_shielded_close_cannot_delay_probe_cleanup(self):
        await self.native_start_cleanup_probe()

    async def native_start_cleanup_probe(self, *, hold_cleanup_receipt=False):
        """Actual Native/RPC cleanup, including a deliberately withheld receipt."""
        import asyncio
        import time
        from teams_transcription.b_native import NativeLiveResponder
        instances = []
        entered = asyncio.Event()
        child = await asyncio.create_subprocess_exec('sleep', '30')
        unrelated = await asyncio.create_subprocess_exec('sleep', '30')
        async def initializing():
            entered.set()
            await asyncio.Event().wait()
        async def closing():
            await asyncio.Event().wait()
        def native_factory(*args, **kwargs):
            native = NativeLiveResponder(*args, **kwargs)
            native.rpc.process = child
            native.rpc.start = initializing
            native.rpc._close = closing
            native.peer = AsyncMock()  # No browser, SDP, media or network.
            instances.append(native)
            return native
        version = SimpleNamespace(returncode=0, communicate=AsyncMock(
            return_value=(b'codex-cli 0.159.0-alpha.3\n', b'')))
        async def inline_io(function, *args, **kwargs): return function(*args, **kwargs)
        browser = AsyncMock(); browser.browser = object()
        browser.playwright = browser._starter = None
        guard = SimpleNamespace(prepare=AsyncMock(), adapter=Mock(return_value=object()))
        actual_cleanup = qa_probe.cleanup_probe
        async def cleanup_with_withheld_receipt(*args, **kwargs):
            result = await actual_cleanup(*args, **kwargs)
            if hold_cleanup_receipt:
                await asyncio.Event().wait()
            return result
        cleanup = AsyncMock(side_effect=cleanup_with_withheld_receipt)
        operation_task = None
        run = None
        try:
            with patch('asyncio.to_thread', side_effect=inline_io), \
                    patch('teams_transcription.b_native.asyncio.create_subprocess_exec',
                          new=AsyncMock(return_value=version)), \
                    patch.object(qa_probe, 'WORK_SECONDS', .03), \
                    patch.object(qa_probe, 'CLEANUP_SECONDS', .05), \
                    patch.object(qa_probe, 'load_assistant_settings', return_value=SimpleNamespace(
                        allowed_egress_hosts=['existing.test'], codex_bin='never-called')), \
                    patch.object(qa_probe, 'synthetic_pcm', return_value=b'\1\0'), \
                    patch.object(qa_probe, 'Browser', return_value=browser), \
                    patch.object(qa_probe, 'RuntimeLiveGuard', return_value=guard), \
                    patch.object(qa_probe, 'NativeLiveResponder', side_effect=native_factory), \
                    patch.object(qa_probe, 'cleanup_probe', cleanup):
                run = asyncio.create_task(qa_probe.run_probe(self.args()))
                # This assertion concerns shielded RPC cleanup, not setup's
                # synchronous SQLite/private-receipt fsync latency under load.
                # Keep the actual work/cleanup budgets and 120ms bound intact.
                await asyncio.wait_for(entered.wait(), timeout=5)
                started = time.monotonic()
                done, _ = await asyncio.wait({run}, timeout=.12)
                self.assertIn(run, done, 'shielded startup close delayed outer cleanup')
                result = run.result()
                elapsed = time.monotonic() - started
            self.assertTrue(entered.is_set(), 'must reach actual LiveSpeechChannel.start')
            cleanup.assert_awaited_once()
            operation_task = cleanup.call_args.kwargs['operation_task']
            self.assertTrue(operation_task.done())  # Before any test fallback.
            self.assertLess(elapsed, .12)
            self.assertEqual(result['failure']['phase'], 'qa_start')
            self.assertEqual(result['failure']['error_code'], 'work_deadline_exceeded')
            self.assertFalse(result['accepted'])
            self.assertEqual(result['owned_cleanup']['status'], 'unknown')
            self.assertFalse(result['owned_cleanup']['pending_operation'])
            self.assertEqual(result['owned_cleanup']['pending_close_tasks'], 0)
            self.assertTrue(result['owned_cleanup']['reaped'])
            self.assertIsNotNone(child.returncode)
            self.assertIsNone(unrelated.returncode)
            self.assertTrue(instances[0]._close_task.done())
            self.assertTrue(instances[0].rpc._close_task.done())
            self.assertFalse(result['owned_cleanup']['stop_ack_received'])
            self.assertFalse(result['owned_cleanup']['stop_closed_received'])
        finally:
            # Keep a failing regression from leaking shielded startup cleanup.
            tasks = [run, operation_task]
            if cleanup.call_args:
                tasks.append(cleanup.call_args.kwargs.get('operation_task'))
            for native in instances:
                tasks.extend([native._close_task, native.rpc._close_task])
            for task in tasks:
                if task and not task.done(): task.cancel()
            await asyncio.sleep(0)
            for native in instances:
                for task in (native._close_task, native.rpc._close_task):
                    if task and not task.done(): task.cancel()
            await asyncio.sleep(0)
            for process in (child, unrelated):
                if process.returncode is None: process.kill()
                await process.wait()


    async def test_private_output_required_before_config_synthesis_or_model(self):
        args = self.args(); args.private_result = None
        with patch.object(qa_probe, 'load_assistant_settings') as settings, \
                patch.object(qa_probe, 'synthetic_pcm') as synth, \
                patch.object(qa_probe, 'NativeLiveResponder') as native:
            with self.assertRaisesRegex(ValueError, 'private_result_path_required'):
                await qa_probe.run_probe(args)
        settings.assert_not_called(); synth.assert_not_called(); native.assert_not_called()

    async def test_private_output_conflict_preserves_existing_file_before_model(self):
        self.private_path.write_text('keep original evidence')
        with patch.object(qa_probe, 'load_assistant_settings') as settings:
            with self.assertRaises(FileExistsError):
                await qa_probe.run_probe(self.args())
        settings.assert_not_called()
        self.assertEqual(self.private_path.read_text(), 'keep original evidence')


    async def test_private_receipt_preserves_wrong_answers_without_useful_latency_claim(self):
        import json
        result = await self.failure_probe(mode='wrong_answer')
        private = json.loads(self.private_path.read_text())
        self.assertEqual(len(private['rounds']), 2)
        for row in private['rounds']:
            self.assertEqual(row['answer_text'], '原樣保留：預算九萬元，並無來源支持。')
            self.assertIsNone(row['first_useful_text_method'])
            self.assertIsNone(row['timing']['first_useful_text_seconds'])
            self.assertIsNone(row['timing']['first_useful_text_from_question_send_begin_seconds'])
        self.assertTrue(all(not check['expected_fact'] for check in result['checks']))
        self.assertNotIn('九萬元', json.dumps(result, ensure_ascii=False))


    async def test_wake_phase_timeout_is_distinct_from_whole_work_deadline(self):
        result = await self.failure_probe(mode='wake_timeout')
        self.assertEqual(result['failure']['phase'], 'wake_revision_1')
        self.assertEqual(result['failure']['error_code'], 'phase_wait_timeout')
        self.assertEqual(result['failure']['timeout_layer'], 'phase_wait')
        self.assertEqual(result['failure']['timeout_seconds'], .01)
        self.assertEqual(result['work_limit_seconds'], 1)

    async def test_inner_start_timeout_is_not_reported_as_work_deadline(self):
        result = await self.failure_probe(mode='start_timeout')
        self.assertEqual(result['failure']['phase'], 'qa_start')
        self.assertEqual(result['failure']['error_code'], 'probe_operation_timeout')


    async def test_candidate_profile_is_explicit_and_diagnostics_keep_native_wake_question_route(self):
        await self.mock_success_probe(text_gate=True, media_profile='trusted-none')

    async def test_unknown_media_profile_refused_before_config_or_model(self):
        args = self.args(); args.peer_media_profile = 'unsupported'
        with patch.object(qa_probe, 'load_assistant_settings') as settings:
            with self.assertRaisesRegex(ValueError, 'unknown_peer_media_profile'):
                await qa_probe.run_probe(args)
        settings.assert_not_called()
