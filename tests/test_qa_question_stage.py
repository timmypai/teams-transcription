"""Explicit question-only diagnostic: actual Store/context, fake provider, no wake claim."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from teams_transcription import qa_probe
from teams_transcription.b_native import NativeLiveResponder


class QuestionStageTests(unittest.IsolatedAsyncioTestCase):
    async def _exercise_text_only_two_revisions(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);sent=[];synthesized=[]
            clip=b'\x00\x10\x00\xf0'*240
            case=self
            args=SimpleNamespace(authorize_live=True,private_result=root/'private.json',
                peer_media_profile='legacy',qa_start_stage='question',config='existing.yml',
                chromium=None,ice_servers_file=None)
            class FakeNative(NativeLiveResponder):
                def __init__(self,*args,**kwargs):
                    super().__init__(*args,**kwargs)
                    self.rpc=SimpleNamespace(process=None,tasks=[])
                    self.peer=AsyncMock()
                    self.peer.stats.return_value={'connection':'connected','data_channel':'open'}
                    # The fake peer still owns a native observation endpoint.
                    # No answer media is produced by this text-only fixture.
                    self.peer.page = SimpleNamespace(
                        evaluate=AsyncMock(return_value=dict(connected=True,
                            packets=0, frames=0, energy=0., duration=0.,
                            source_packets=0, source_frames=0,
                            source_energy=0., source_duration=0.)),
                        is_closed=Mock(return_value=False))
                    self.wire_start=0
                    async def received(pcm):
                        sent.append(pcm)
                        if any(pcm):
                            await self.handle_notification('thread/realtime/transcript/delta',
                                {'role':'user','threadId':self.thread_id,'delta':qa_probe.QUESTION})
                    self.peer.push.side_effect=received
                async def start(self,enable_output=False):
                    await self.output.attach_live(self.peer.page)
                    self.live_start_params('private-sdp')
                    self.thread_id='owned-thread'
                    self.started=True
                    self.state='sleeping'
                    self.context.bind_session(self.thread_id)
                    self._answer=asyncio.get_running_loop().create_future()
                    self._rx_task=asyncio.create_task(self._pump_rx())
                    self._question_task=asyncio.create_task(self._poll_questions())
                async def _begin_native_speech(self,state):
                    case.assertEqual(state,'answering')
                    self.state=state
                    await self._begin_output_speech()
                    async def provider_answer():
                        await asyncio.sleep(0)
                        # Native output opens before RESULT_COMPLETE is sent.
                        events=[call.args[0] for call in self.peer.send_event.await_args_list[self.wire_start:]]
                        self.wire_start=self.peer.send_event.await_count
                        case.assertEqual(events[-1]['content'][0]['text'],'RESULT_COMPLETE')
                        payload=json.loads(''.join(event['content'][0]['text'] for event in events[:-1]))
                        text=next(row['text'] for row in payload['selected_source_reading']
                            if '預算' in row.get('text',''))
                        await self.handle_notification('thread/realtime/transcript/done',
                            {'role':'assistant','threadId':self.thread_id,'text':text})
                        await self._end_output_speech()
                        self.state='sleeping'
                    self._speech_task=asyncio.create_task(provider_answer())
                async def stop(self):
                    self._closing=True
                    tasks=[task for task in (self._rx_task,self._question_task,
                        self._tool_task,self._speech_task,self._phase_timeout) if task is not None]
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
                    if not self._answer.done():
                        self._answer.cancel()
                    await self.output.close()
                    self.stop_ack_received=self.stop_closed_received=True
                    self.cleanup_status='cleaned'
                    self.state='stopped'
                    return 'stopped'
            def synth(text):
                synthesized.append(text)
                return clip
            async def inline(fn,*a,**kw):return fn(*a,**kw)
            guard=SimpleNamespace(prepare=AsyncMock(),adapter=Mock(return_value=object()))
            browser=AsyncMock();browser.browser=object();browser.playwright=browser._starter=None
            with patch('asyncio.to_thread',side_effect=inline), \
                 patch.object(qa_probe,'synthetic_pcm',side_effect=synth), \
                 patch.object(qa_probe,'NativeLiveResponder',FakeNative), \
                 patch.object(qa_probe,'load_assistant_settings',return_value=SimpleNamespace(allowed_egress_hosts=('existing.example',),codex_bin='codex')), \
                 patch.object(qa_probe,'load_ice_servers',return_value=[]), \
                 patch.object(qa_probe,'RuntimeLiveGuard',return_value=guard), \
                 patch.object(qa_probe,'Browser',return_value=browser):
                result=await qa_probe.run_probe(args)
            private=json.loads((root/'private.json').read_text())
            # This scripted provider emits text only. Validate its semantic and
            # cleanup path without claiming the separate audio acceptance gate.
            self.assertIsNone(result['failure'])
            self.assertTrue(result['owned_cleanup_verified'])
            self.assertEqual(synthesized,[qa_probe.QUESTION])
            self.assertEqual([pcm for pcm in sent if any(pcm)],[clip,clip])
            self.assertTrue(any(not any(pcm) for pcm in sent))
            self.assertEqual(result['wake_validation'],'skipped_unverified')
            self.assertNotIn('exact_greeting',result['acceptance_criteria'])
            self.assertTrue(all(c['exact_greeting'] is None for c in result['checks']))
            self.assertEqual(result['media_diagnostics']['wake_matches'],0)
            self.assertEqual(result['media_diagnostics']['greeting_entries'],0)
            self.assertEqual(result['media_diagnostics']['user_transcript_events'],2)
            self.assertEqual(private['request']['diagnostic_mode'],'synthetic_question_only')
            self.assertFalse(private['request']['provider_wake_skipped'])
            self.assertTrue(private['request']['local_wake_skipped'])
            self.assertNotIn('private-sdp',json.dumps(private))
            self.assertEqual(result['private_evidence']['sha256'],hashlib.sha256((root/'private.json').read_bytes()).hexdigest())
            self.assertIn('十萬元',private['rounds'][0]['answer_text'])
            self.assertIn('十二萬元',private['rounds'][1]['answer_text'])
            self.assertFalse(result['context']['model_consumption_verified'])
            self.assertEqual(result['question_receipts']['counts'],
                {'received':2,'admitted':2,'rejected':0})
            self.assertTrue(all(row['kind']=='delta'
                for round in private['rounds'] for row in round['user_transcripts']))
            return result

    async def test_question_only_two_revisions_frame_actual_pcm_without_provider_final(self):
        result=await self._exercise_text_only_two_revisions()
        self.assertEqual([row['source_version'] for row in result['checks']],[2,3])
        for row in result['checks']:
            for criterion in ('scoped_tool_called','expected_fact','semantic_content_verified',
                    'no_stale_fact','output_muted','exact_source_quote'):
                with self.subTest(source_version=row['source_version'],criterion=criterion):
                    self.assertTrue(row[criterion])

    async def test_text_only_semantic_success_cannot_pass_overall_audio_acceptance(self):
        result=await self._exercise_text_only_two_revisions()
        semantic=('scoped_tool_called','expected_fact','semantic_content_verified',
            'no_stale_fact','output_muted')
        self.assertEqual(result['acceptance_criteria'],list(semantic)+['audio_rx_verified'])
        self.assertEqual(len(result['checks']),2)
        for row in result['checks']:
            self.assertTrue(all(row[key] for key in semantic))
            self.assertFalse(row['audio_rx_verified'])
            self.assertEqual(row['audio_rx']['reason'],'incomplete_answer_window')
        self.assertIsNone(result['failure'])
        self.assertTrue(result['owned_cleanup_verified'])
        self.assertFalse(result['accepted'])  # No answer audio was produced.

    async def test_unknown_stage_refused_before_config_or_model(self):
        args=SimpleNamespace(authorize_live=True,private_result=Path('/tmp/not-created-by-question-test'),
            peer_media_profile='legacy',qa_start_stage='unknown')
        with patch.object(qa_probe,'load_assistant_settings',side_effect=AssertionError('no config')):
            with self.assertRaisesRegex(ValueError,'unknown_qa_start_stage'):
                await qa_probe.run_probe(args)


if __name__=='__main__':unittest.main()
