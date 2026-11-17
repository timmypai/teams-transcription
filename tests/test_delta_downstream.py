"""Saved-source provenance and summary acceptance; no model, meeting or audio."""
import tempfile
import unittest
from unittest.mock import patch

from teams_transcription.summary import SECTIONS,generate,source_quality
from teams_transcription.b_response import MeetingMinutesTool,MeetingStoreMinutesReader
from teams_transcription.storage import Session


def delta_entry(ident,text,**kwargs):
    return {'id':ident,'text':text,'source_state':'application_segment','provider_final':False,
        'quality':'unfinalized','timestamp':'2026-10-10T14:00:00+00:00','source_timestamp':None,
        'received_offset_ms':1200,'ordering':'receipt_order','deduplication':'source_identity_only',
        'source_event_id':None,'source_revision':None,'source_sequence':None,**kwargs}


def saved_session(root,provider='mock'):
    session = Session(root).create({'provider':provider,'source':'synthetic'})
    for receipt,text in enumerate(['上週完成測試。','本週修正保存流程。'],1):
        session.record_increment(dict(capture_id='a'*32,receipt_sequence=receipt,part_index=0,
            text=text,received_at='2026-10-10T14:00:00+00:00'))
    return session


class DeltaSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_delta_only_summary_preserves_citations_without_promoting_source_finality(self):
        with tempfile.TemporaryDirectory() as root:
            session = saved_session(root)
            session.finalize_minutes('stopped')
            before = session.store.read(session.ident)
            value = await generate(session,session.path/'unused')
            self.assertEqual([item['text'] for item in value['重點']],['上週完成測試。','本週修正保存流程。'])
            stored = session.store.read(session.ident)
            self.assertEqual(stored['summary'],value)
            self.assertEqual(stored['segments'],before['segments'])
            self.assertFalse(value['summary_state']['provider_requirement_met'])
            quality = value['source_quality']
            self.assertEqual(quality['provider_final_count'],0)
            self.assertEqual(quality['unfinalized_source_count'],2)
            self.assertFalse(quality['all_sources_provider_final'])
            self.assertEqual({item['sources'][0] for item in value['重點']},{entry['id'] for entry in session.entries()})

    async def test_unsealed_source_is_rejected_before_any_model_or_mock_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            session = saved_session(root,provider='codex')
            with patch('teams_transcription.rpc.CodexRPC') as rpc,patch('teams_transcription.summary.mock_summary') as fallback:
                with self.assertRaisesRegex(ValueError,'summary_requires_terminal'):
                    await generate(session,session.path/'unused')
            rpc.assert_not_called()
            fallback.assert_not_called()
            self.assertIsNone(session.store.summary_state(session.ident))

    def test_quality_never_infers_finality_from_flags_or_legacy_text(self):
        entries = [delta_entry('D1','draft',provider_final=True),
            delta_entry('H42','legacy',source_state='legacy_provisional',quality='legacy_truncated'),
            delta_entry('F1','final',source_state='provider_final',provider_final=True,quality='provider_final'),
            {'id':'old','text':'unknown provenance'}]
        quality = source_quality(entries)
        self.assertEqual(quality['provider_final_count'],1)
        self.assertEqual(quality['unfinalized_source_count'],3)
        self.assertFalse(quality['all_sources_provider_final'])
        self.assertEqual(quality['source_state_counts'],dict(provider_final=1,application_segment=1,legacy_provisional=1,unknown=1))
        self.assertEqual(quality['quality_counts']['legacy_truncated'],1)

    async def test_controlled_summary_cannot_assert_host_quality_or_foreign_citations(self):
        for extra in ({'source_quality':{'all_sources_provider_final':True}},
                {'重點':[dict(text='invented',sources=['absent'],owner='',date='')]}):
            with tempfile.TemporaryDirectory() as root:
                session = saved_session(root)
                session.finalize_minutes('stopped')
                candidate = {**{key:[] for key in SECTIONS},**extra}
                with patch('teams_transcription.summary.mock_summary',return_value=candidate):
                    with self.assertRaises(ValueError):
                        await generate(session,session.path/'unused')
                self.assertIsNone(session.store.summary_state(session.ident))


class DeltaContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_unfinalized_context_is_searchable_but_not_provider_final(self):
        with tempfile.TemporaryDirectory() as root:
            session = saved_session(root)
            session.finalize_minutes('stopped')
            tool = MeetingMinutesTool(session.ident,MeetingStoreMinutesReader(session.store))
            result = await tool.dispatch(tool.name,{'query':'修正'})
            self.assertTrue(result['finalized'])
            self.assertEqual(result['completeness'],'partial')
            self.assertEqual(result['transcript_state']['provider_final_count'],0)
            self.assertIn('本週修正保存流程。',[row['text'] for row in result['context']])
            self.assertTrue(all(not locator['provider_final'] for locator in result['source_locators']))
            self.assertEqual([row['text'] for row in result['context']],
                [source['text'] for source in session.store.read(session.ident)['sources']])
