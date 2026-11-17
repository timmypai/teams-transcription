"""Durable mode requires explicit source identity/revision, never text guesses."""
import tempfile
import unittest
from unittest.mock import AsyncMock
from teams_transcription.live import LiveProvider, TranscriptNormalizer

class DurableFinalIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_durable_live_keeps_idless_rpc_dc_and_same_channel_repeats(self):
        saved = []
        async def final(event):
            saved.append(event)
        with tempfile.TemporaryDirectory() as root:
            provider = LiveProvider(None,root,AsyncMock(),AsyncMock(),on_increment=AsyncMock(),on_final=final)
            provider.thread_id = 'own'
            await provider._source('thread/realtime/transcript/done',{'threadId':'own','role':'user','text':'重複原文'})
            await provider._source('turn.done',{'turn':{'role':'user','transcript':'重複原文'}})
            await provider._source('thread/realtime/transcript/done',{'threadId':'own','role':'user','text':'重複原文'})
            self.assertEqual(len(saved),3)
            self.assertEqual(len({v['event_id'] for v in saved}),3)

    async def test_verified_revision_is_idempotent_but_missing_revision_is_not_guessed(self):
        normalizer = TranscriptNormalizer()
        normalizer.strict_identity = True
        params = {'itemId':'stable','role':'user','text':'版本原文'}
        first = normalizer.final_event('thread/realtime/transcript/done',params,1,'own')
        second = normalizer.final_event('thread/realtime/transcript/done',params,1.1,'own')
        self.assertNotEqual(first['event_id'],second['event_id'])
        verified = normalizer.final_event('thread/realtime/transcript/done',{**params,'revision':0},2,'own')
        self.assertIsNone(normalizer.final_event('thread/realtime/transcript/done',{**params,'revision':0},2.1,'own'))
        changed = normalizer.final_event('thread/realtime/transcript/done',{**params,'revision':1,'text':'明確更正'},3,'own')
        self.assertEqual(verified['event_id'],changed['event_id'])
        self.assertEqual(changed['revision'],1)
        with self.assertRaisesRegex(ValueError,'revision_conflict'):
            normalizer.final_event('thread/realtime/transcript/done',{**params,'revision':1,'text':'衝突'},4,'own')
