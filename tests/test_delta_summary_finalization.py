"""Large source-count finalization without automatic worker summary.

No browser, Teams, physical audio, Codex process, credential, or network is used.
The production store saves the 4,000 tiny application segments in one SQL batch.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from teams_transcription import summary
from teams_transcription.b_response import MeetingMinutesTool, MeetingStoreMinutesReader
from teams_transcription.live import LiveProvider
from teams_transcription.meeting_store import MeetingStore
from teams_transcription.response_runtime import _AsyncStore
from teams_transcription.storage import Session
from teams_transcription.worker import run_session
from test_rx_contract import Fixture, config, native_transport


class DeltaSummaryFinalizationTests(unittest.IsolatedAsyncioTestCase):
    def assert_bounded_source_quality(self, quality):
        self.assertEqual(quality['version'], 2)
        self.assertEqual(quality['artifact_kind'], 'derived_summary')
        self.assertEqual(quality['saved_source_count'], 4000)
        self.assertEqual(quality['provider_final_count'], 0)
        self.assertEqual(quality['unfinalized_source_count'], 4000)
        self.assertEqual(quality['source_state_counts']['application_segment'], 4000)
        self.assertEqual(quality['source_state_counts']['provider_final'], 0)
        self.assertEqual(quality['quality_counts']['unfinalized'], 4000)
        self.assertEqual(quality['ordering_counts']['receipt_order'], 4000)
        self.assertEqual(quality['source_clock_known_count'], 0)
        self.assertEqual(quality['received_clock_known_count'], 4000)
        self.assertEqual(quality['verified_source_identity_count'], 0)
        self.assertFalse(quality['all_sources_provider_final'])
        self.assertNotIn('references', quality)
        self.assertLess(len(json.dumps(quality, ensure_ascii=False).encode()), 16*1024)

    async def test_four_thousand_tiny_sources_finalize_without_automatic_summary(self):
        with tempfile.TemporaryDirectory() as root:
            session=Session(root).create(config(rx_test_contract=False,rx_session_contract=False,
                duration=.01,prior_context={'available':False,'note':'上次結論：保留原文與來源狀態。'}))
            other=Session(root).create(config(rx_test_contract=False))
            async def source(callback, owner): await asyncio.Event().wait()
            fixture = Fixture(emit_final=False, source=source)
            saved_batches = []
            class SyntheticLive(LiveProvider):
                async def start(current):
                    fixture.provider = current
                    fixture.live_starts += 1
                    current.thread_id = 'own'
                    current.peer = SimpleNamespace(request_session_close=AsyncMock(return_value=False),
                        push=AsyncMock(), close=AsyncMock(),
                        stats=AsyncMock(return_value=native_transport()))
                    current.rpc = SimpleNamespace(process=None, close=AsyncMock())
                    current.once = False
                    current.stage = 'connected'
                async def ready(current):
                    if not await super().ready():
                        return False
                    if not current.once:
                        current.once = True
                        value = dict(capture_id=current.capture_id, receipt_sequence=1, part_index=0,
                            text='嗯', parts=['嗯']*4000, received_at='2026-10-10T18:00:00+00:00',
                            source_event_id=None, source_revision=None,
                            source_sequence=None, source_timestamp=None)
                        await current.on_increment(value)
                        saved_batches.append(value)
                    return True
                async def drain(current, seconds): pass
                async def close(current):
                    await super().close()
                    fixture.closed.append('live')

            observed_sources = []
            def controlled_mock(entries):
                observed_sources.extend(entries)
                value = {section:[] for section in summary.SECTIONS}
                value['重點'] = [dict(text='合成短摘要。', sources=[entries[0]['id']], owner='', date='')]
                return value
            real_generate = summary.generate
            async def explicitly_mock_generate(summary_session, *args, **kwargs):
                # Capture remains the injected Live fixture. Only this synthetic
                # summary operation selects mock, without changing saved config
                # or claiming acceptance of the ordinary Codex summary provider.
                fixture_config = {**summary_session.config(), 'provider':'mock'}
                with patch.object(summary_session, 'config', return_value=fixture_config):
                    return await real_generate(summary_session, *args, **kwargs)
            with fixture.install(), patch('teams_transcription.worker.LiveProvider', SyntheticLive), \
                    patch('teams_transcription.worker.summary.generate', new_callable=AsyncMock) as generate, \
                    patch('teams_transcription.rpc.CodexRPC') as rpc, \
                    patch('teams_transcription.summary.mock_summary', side_effect=controlled_mock) as extract:
                self.assertEqual(await run_session(session, install_signals=False), 0)
                generate.assert_not_called()
                extract.assert_not_called()
                rpc.assert_not_called()

            # The worker finalizes all source records without any summarizer.
            snapshot = MeetingStore(root).read(session.ident)
            self.assertTrue(snapshot['terminal'])
            self.assertEqual(snapshot['status'], 'completed')
            self.assertEqual(session.status()['state'], 'completed')
            self.assertEqual(session.status()['summary_state'], 'not_requested')
            self.assertNotIn('summary_state',snapshot)
            self.assertFalse((session.path/'summary.md').exists())
            self.assertFalse((session.path/'summary.json').exists())
            self.assertEqual(len(saved_batches), 1)
            self.assertEqual(len(snapshot['segments']), 4000)
            self.assertEqual(len(snapshot['increments']), 4000)
            self.assertEqual(''.join(item['text'] for item in snapshot['segments']), '嗯'*4000)
            self.assertEqual(snapshot['events'], [])
            self.assertEqual(snapshot['transcript_state']['provider_final_count'], 0)
            self.assertEqual(snapshot['transcript_state']['unfinalized_segment_count'], 4000)
            self.assertTrue(snapshot['transcript_state']['meeting_ended'])

            # Preserve the separate public mock API's nonempty citation and
            # bounded metadata coverage. This explicit test operation happens
            # after the worker has finished; it is never a service invocation.
            with patch('teams_transcription.rpc.CodexRPC') as explicit_rpc, \
                    patch('teams_transcription.summary.mock_summary', side_effect=controlled_mock) as extract:
                await explicitly_mock_generate(session, session.path/'unused-model-runtime', 'never-called')
            explicit_rpc.assert_not_called()
            self.assertEqual(len(observed_sources), 4000)
            self.assertEqual(''.join(item['text'] for item in observed_sources), '嗯'*4000)
            self.assertTrue(all(item['source_state'] == 'application_segment' for item in observed_sources))
            extract.assert_called_once()
            rpc.assert_not_called()
            self.assertEqual(session.config()['provider'], 'codex')

            saved_summary=MeetingStore(root).read(session.ident)['summary']
            quality = saved_summary['source_quality']
            self.assert_bounded_source_quality(quality)
            recovered = MeetingStore(root).read(session.ident)
            self.assertFalse(recovered['summary_state']['provider_requirement_met'])
            self.assertTrue(recovered['terminal'])
            self.assertEqual(recovered['status'], 'completed')
            self.assertEqual(recovered['segments'], snapshot['segments'])
            self.assertEqual(recovered['increments'], snapshot['increments'])
            self.assertEqual(recovered['prior_context'],snapshot['prior_context'])
            self.assertEqual(recovered['events'], snapshot['events'])
            original_ids = {entry['id'] for entry in session.entries()}
            self.assertEqual(len(original_ids), 4000)
            for item in saved_summary['重點']:
                for ident in item['sources']:
                    self.assertIn(ident, original_ids)

            reader = MeetingStoreMinutesReader(_AsyncStore(root))
            source_view = await reader(session.ident)
            self.assertEqual(source_view['meeting_id'], session.ident)
            self.assertEqual(source_view['transcript_state']['unfinalized_segment_count'], 4000)
            self.assertEqual(''.join(row['text'] for row in source_view['records']),'嗯'*4000)
            tool=MeetingMinutesTool(session.ident,reader)
            result=await tool.dispatch(tool.name,{'query':'嗯'})
            self.assertTrue(result['available'])
            self.assertEqual(result['meeting_id'], session.ident)
            self.assertEqual(result['transcript_state']['provider_final_count'], 0)
            self.assertEqual((await reader(other.ident))['records'],[])
            self.assertTrue(all(name in fixture.closed for name in ('capture','meeting','live','browser')))
            self.assertTrue(fixture.source_task.done())
            self.assertFalse(list(Path(root).rglob('*.wav')))

    async def test_public_api_four_thousand_receipts_empty_mock_summary_finalizes(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(rx_test_contract=False, provider='mock'))
            store = MeetingStore(root)
            for receipt in range(1, 4001):
                store.record_increment(session.ident, dict(capture_id='d'*32,
                    receipt_sequence=receipt, part_index=0, text='嗯',
                    received_at='2026-10-10T18:00:00+00:00'), publish=False)
            observed_sources = []
            def controlled_empty_mock(entries):
                observed_sources.extend(entries)
                return {section:[] for section in summary.SECTIONS}
            session.finalize_minutes('completed')
            with patch('teams_transcription.rpc.CodexRPC') as rpc, \
                    patch('teams_transcription.summary.mock_summary', side_effect=controlled_empty_mock) as extract:
                result = await summary.generate(session, session.path/'unused-model-runtime', 'never-called')
            self.assert_bounded_source_quality(result['source_quality'])
            self.assertEqual(len(observed_sources), 4000)
            self.assertEqual(''.join(item['text'] for item in observed_sources), '嗯'*4000)
            for section in summary.SECTIONS:
                self.assertEqual(result[section], [])
            completed=session.finalize_minutes('completed')
            self.assertTrue(completed['terminal'])
            self.assertEqual(completed['status'], 'completed')
            self.assertFalse(completed['summary_state']['provider_requirement_met'])
            recovered = MeetingStore(root).read(session.ident)
            self.assertEqual(len(recovered['increments']), 4000)
            self.assertEqual(len(recovered['segments']), 4000)
            self.assertEqual(''.join(item['text'] for item in recovered['segments']), '嗯'*4000)
            self.assertEqual(recovered['transcript_state']['provider_final_count'], 0)
            self.assertEqual(recovered['transcript_state']['quality'], 'includes_unfinalized')
            self.assertEqual(len(session.entries()), 4000)
            self.assertEqual(recovered['summary']['source_quality'],result['source_quality'])
            extract.assert_called_once()
            rpc.assert_not_called()
            self.assertFalse(list(Path(root).rglob('*.wav')))
