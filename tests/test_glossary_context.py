import copy
import json
import tempfile
import unittest
from unittest.mock import AsyncMock

from teams_transcription.assistant_store import AssistantStore
from teams_transcription.glossary_context import glossary_initial_items
from teams_transcription.live import LiveProvider


class GlossaryContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AssistantStore(self.tmp.name)
        self.entry = self.store.save_glossary(dict(kind='term',canonical='RTO',aliases=['阿體歐'],
            pronunciation='ar tee oh',definition='recovery time objective',status='active'))
        self.snapshot = self.store.glossary_snapshot(None)

    async def test_frozen_vocabulary_never_rewrites_saved_provider_speech(self):
        finals = AsyncMock()
        provider = LiveProvider(None,self.tmp.name,AsyncMock(),AsyncMock(),on_final=finals,
            glossary_snapshot=self.snapshot)
        original_items = copy.deepcopy(provider.glossary_items)
        self.snapshot['entries'][0]['canonical'] = 'Changed after freeze'
        provider.thread_id = 'own-thread'
        await provider._source('thread/realtime/transcript/done',
            {'role':'user','itemId':'own-item','text':'這裡聽到阿體歐，不代表 RTO。'})
        self.assertEqual(finals.await_args.args[0]['text'],'這裡聽到阿體歐，不代表 RTO。')
        self.assertEqual(provider.glossary_items,original_items)

    def test_tampered_hash_or_revision_is_rejected_before_start(self):
        altered = copy.deepcopy(self.snapshot)
        altered['entries'][0]['canonical'] = 'Different'
        with self.assertRaisesRegex(ValueError,'invalid_glossary_snapshot'):
            glossary_initial_items(altered)
        altered = copy.deepcopy(self.snapshot)
        altered['entries'][0]['revision'] = True
        with self.assertRaisesRegex(ValueError,'invalid_glossary_snapshot'):
            glossary_initial_items(altered)

    def test_all_large_approved_vocabulary_reaches_frozen_provider_initial_items(self):
        expected = {'RTO':('recovery time objective',self.entry['revision'])}
        for index in range(50):
            canonical = f'Controlled term {index}'
            definition = f'Definition {index}: '+' '.join(['Long controlled definition']*21)
            entry = self.store.save_glossary(dict(kind='term',canonical=canonical,
                definition=definition,status='active'))
            expected[canonical] = (definition,entry['revision'])
        snapshot = self.store.glossary_snapshot(None)
        provider = LiveProvider(None,self.tmp.name,AsyncMock(),AsyncMock(),glossary_snapshot=snapshot)
        metadata = json.loads(provider.glossary_items[0]['text'].split('\n',1)[1])
        self.assertEqual({entry['canonical']:(entry['definition'],entry['revision'])
            for entry in metadata['entries']},expected)
        self.assertFalse(metadata['truncated'])
        self.assertEqual(metadata['omitted_count'],0)
        self.assertEqual(metadata['total_relevant'],51)
        self.assertEqual((metadata['version'],metadata['sha256']),(snapshot['version'],snapshot['sha256']))
        self.store.set_glossary_status(self.entry['id'],'disabled')
        self.assertIn('RTO',[entry['canonical'] for entry in
            json.loads(provider.glossary_items[0]['text'].split('\n',1)[1])['entries']])

    def test_candidates_are_excluded_until_explicit_approval(self):
        self.store.set_glossary_status(self.entry['id'],'pending')
        self.assertEqual(glossary_initial_items(self.store.glossary_snapshot(None)),[])
        approved = self.store.set_glossary_status(self.entry['id'],'active')
        snapshot = self.store.glossary_snapshot(None)
        self.assertEqual(snapshot['entries'][0]['revision'],approved['revision'])


if __name__=='__main__':
    unittest.main()
