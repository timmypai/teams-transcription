"""Actual B attach/startup/receipt paths; browser and RPC boundaries are synthetic."""
import asyncio
import json
import shutil
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import Error as PlaywrightError

from teams_transcription import b_browser_tx as tx
from teams_transcription import response_runtime
from teams_transcription.b_native import PINNED_CODEX_VERSION
from teams_transcription.browser import RESPONSE_SENDER_JS
from teams_transcription.diagnostics import response_start_receipt
import test_response_start_diagnostics as startup_fixture
from lifecycle_probe import session_events


PRIVATE='PRIVATE-SYNTHETIC token https://teams.microsoft.com/private?token=secret v=0 SDP'


class Page:
    """Simulate only the external Playwright boundary, never execute JS/media."""
    def __init__(self, failure, *, fail_cleanup=False):
        self.failure=failure
        self.fail_cleanup=fail_cleanup
        self.context=object()
        self.error_message=PRIVATE
        self.environment=None

    def is_closed(self):return False

    async def evaluate(self, script, args=None):
        if isinstance(args,dict) and args.get('receipt') is True and 'policyId' in args:
            return {'accepted':True,'reason':'none'}
        steps={tx.SOURCE_OFFER_JS:'source_offer',tx.TARGET_ANSWER_JS:'target_answer',
            tx.SOURCE_ANSWER_JS:'source_answer',tx.READY_TX_JS:'target_ready',
            tx.SOURCE_CHECK_JS:'source_check',tx.START_TX_JS:'sender_install',
            tx.MUTE_TX_JS:'initial_mute'}
        if self.failure is not None and steps.get(script)==self.failure:raise PlaywrightError(self.error_message)
        if script==tx.TARGET_SETUP_SNAPSHOT_JS:return self.environment
        if script==RESPONSE_SENDER_JS:
            return {'candidates':[{'peer':0,'sender':0}],'diagnostic':{}}
        if script==tx.BIND_TX_JS:return {'owner':args['owner'],'meeting_id':args['meetingId']}
        if script==tx.STOP_TX_JS:
            if self.fail_cleanup:raise OSError(PRIVATE)
            return {'cleaned':True}
        if script==tx.START_TX_JS:return {'muted':True,'track_id':'synthetic-track'}
        if script in (tx.SOURCE_OFFER_JS,tx.TARGET_ANSWER_JS):return 'synthetic-sdp'
        return True


class NativeOutputAttachDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_during_readonly_snapshot_still_finishes_owned_cleanup(self):
        entered=asyncio.Event()
        class SnapshotPage(Page):
            async def evaluate(self,script,args=None):
                if script==tx.TARGET_SETUP_SNAPSHOT_JS:
                    entered.set();await asyncio.Future()
                return await super().evaluate(script,args)
        target,source=SnapshotPage('target_answer'),Page(None)
        sink=tx.BrowserTXSink(target,meeting_id='one');await sink.bind()
        task=asyncio.create_task(sink.attach_live(source));await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(sink.cleanup_status,'cleaned')
        self.assertFalse(sink._pending)
        self.assertFalse(sink.started)
        self.assertEqual(sink.startup_error['output_step'],'target_answer')

    async def test_target_failure_retains_first_environment_and_policy_without_changing_cleanup(self):
        source,target=Page(None),Page('target_answer')
        target.error_message='Frame.evaluate: Error: native_tx_setup/decoder_setup/Error/decoder/worklet_module/AbortError/unknown'
        target.environment={'secure_context':True,'audio_decoder':True,'context_audio_worklet':True,
            'context_state':'running','sample_rate':48000,'private':PRIVATE}
        sink=tx.BrowserTXSink(target,meeting_id='one',document_policy=lambda:{'available':False})
        await sink.bind()
        with self.assertRaises(PlaywrightError):await sink.attach_live(source)
        self.assertEqual(sink.startup_error['decoder_stage'],'worklet_module')
        self.assertTrue(sink.startup_error['target_environment']['audio_decoder'])
        self.assertTrue(sink.startup_error['target_environment']['context_audio_worklet'])
        self.assertEqual(sink.startup_error['target_document_policy'],{'available':False})
        self.assertEqual(response_start_receipt(sink.startup_error),sink.startup_error)
        self.assertEqual(sink.cleanup_status,'cleaned')
        self.assertFalse(sink.started)
        self.assertNotIn(PRIVATE,json.dumps(sink.startup_error))

    def test_fixed_browser_reason_survives_without_exception_message_or_stack(self):
        from teams_transcription.diagnostics import response_start_failure
        cases={
            'Native Live audio receiver unavailable':'live_receiver_unavailable',
            'No-PLC Live decoder unavailable':'live_decoder_unavailable',
            'Native output ICE timeout':'relay_ice_timeout',
            'TX document/meeting/peer binding invalidated':'target_binding_invalidated',
            'b_no_plc_setup_failed':'decoder_setup_failed',
            'Native TX relay not ready':'relay_not_ready',
        }
        for message,code in cases.items():
            for prefix in ('', 'Page.evaluate: Error: ', 'Frame.evaluate: Error: '):
                with self.subTest(message=message,prefix=prefix):
                    failure=PlaywrightError(prefix+message+'\n    at '+PRIVATE)
                    receipt=response_start_failure(failure,'native_output_attach',output_step='target_answer')
                    self.assertEqual(receipt.get('output_error_code'),code)
                    self.assertEqual(response_start_receipt(receipt),receipt)
                    self.assertNotIn(PRIVATE,json.dumps(receipt))
        for message in (PRIVATE, 'Native output ICE timeout '+PRIVATE,
                        PRIVATE+'\nNative output ICE timeout', 'x'*10000):
            receipt=response_start_failure(PlaywrightError(message),'native_output_attach',output_step='target_answer')
            self.assertNotIn('output_error_code',receipt)
            self.assertNotIn(PRIVATE,json.dumps(receipt))
        receipt=response_start_failure(PlaywrightError('Native output ICE timeout'),'thread_start')
        self.assertNotIn('output_error_code',receipt)

    def test_untrusted_output_reason_is_allowlisted_at_projection_boundary(self):
        receipt=response_start_receipt({'phase':'native_output_attach','exception_type':'Error',
            'output_step':'target_answer','output_error_code':PRIVATE,'message':PRIVATE})
        self.assertEqual(receipt.get('output_error_code'),'unknown')
        self.assertNotIn(PRIVATE,json.dumps(receipt))

    def test_target_stage_type_survive_all_projection_layers(self):
        from teams_transcription.diagnostics import response_start_failure
        for stage in ('remote_description','decoder_setup','local_description','ice_gather'):
            receipt=response_start_failure(PlaywrightError(
                'Frame.evaluate: Error: native_tx_setup/'+stage+'/NotSupportedError\n'+PRIVATE),
                'native_output_attach',output_step='target_answer')
            self.assertEqual(receipt.get('target_stage'),stage)
            self.assertEqual(receipt.get('target_exception_type'),'NotSupportedError')
            self.assertEqual(response_start_receipt(receipt),receipt)
            self.assertNotIn(PRIVATE,json.dumps(receipt))
        receipt=response_start_receipt({'phase':'native_output_attach','exception_type':'Error',
            'output_step':'target_answer','target_stage':PRIVATE,'target_exception_type':{}})
        self.assertEqual(receipt['target_stage'],'unknown')
        self.assertEqual(receipt['target_exception_type'],'unknown')

    @unittest.skipUnless(shutil.which('node'),'Node fixture unavailable')
    def test_actual_target_answer_reports_stage_and_cleans_without_exporting_message(self):
        # Real production JS with synthetic external WebRTC/WebAudio boundaries.
        # This is not a browser or ICE/media success test.
        harness=r'''
          const assert=require('node:assert/strict');
          const source=JSON.parse(process.argv[1]);
          const target=eval('('+source+')');
          (async()=>{
            for(const failure of ['remote_description','receiver_check','decoder_setup','hostile_name']){
              let closes=0;
              global.document={};global.location={href:'https://synthetic.invalid/'};
              global.addEventListener=()=>{};global.removeEventListener=()=>{};
              const original={enabled:false};const sender={track:original};
              const pc={connectionState:'connected',getSenders:()=>[sender]};
              const track={kind:'audio',readyState:'live',enabled:true,
                addEventListener(){},removeEventListener(){},stop(){this.readyState='ended';}};
              const receiver={track,createEncodedStreams(){throw Error('PRIVATE-SETUP');}};
              const relay={addEventListener(){},close(){closes++;},
                async setRemoteDescription(){
                  if(failure==='hostile_name'){
                    const e={};Object.defineProperty(e,'name',{get(){throw Error('PRIVATE-GETTER');}});throw e;
                  }
                  if(failure==='remote_description'){
                    const e=Error('PRIVATE-TOKEN https://private.invalid/sdp');e.name='NotSupportedError';throw e;
                  }
                  if(failure!=='receiver_check')this.ontrack({track,receiver});
                }};
              const capture={peers:[pc],createNativeOutputPeer:()=>relay};
              const binding={document,meetingId:'one',href:location.href,capture,
                peerIndex:0,pc,sender,original};
              global.window={__bMeetingId:'one',__meeting:capture,__bTXBindings:new Map([['owner',binding]])};
              let message;
              try{await target({owner:'owner',sdp:'PRIVATE-SDP'});}catch(e){message=e.message;}
              assert.equal(message,'native_tx_setup/'+(failure==='hostile_name'?'remote_description':failure)+'/'+
                (failure==='remote_description'?'NotSupportedError':failure==='hostile_name'?'unknown':'Error')+
                (failure==='decoder_setup'?'/decoder/browser_api/Error/api_unsupported':''));
              assert.ok(closes>0);assert.equal(window.__bTX.disposed,true);
              assert.equal(sender.track,original);assert.equal(sender.track.enabled,false);
              assert.ok(!message.includes('PRIVATE'));
            }
          })().catch(e=>{console.error(e);process.exitCode=1;});
        '''
        result=subprocess.run(['node','-e',harness,json.dumps(tx.TARGET_ANSWER_JS)],
            capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_decoder_first_cause_survives_fixed_target_receipt_without_raw_text(self):
        from teams_transcription.diagnostics import response_start_failure
        error=PlaywrightError('Frame.evaluate: Error: native_tx_setup/decoder_setup/Error/'
            'decoder/worklet_module/AbortError/unknown\n'+PRIVATE)
        receipt=response_start_failure(error,'native_output_attach',output_step='target_answer')
        self.assertEqual(receipt.get('decoder_stage'),'worklet_module')
        self.assertEqual(receipt.get('decoder_exception_type'),'AbortError')
        self.assertEqual(receipt.get('decoder_reason'),'unknown')
        self.assertEqual(response_start_receipt(receipt),receipt)
        self.assertNotIn(PRIVATE,json.dumps(receipt))
        hostile={**receipt,'decoder_stage':PRIVATE,'decoder_exception_type':{},'decoder_reason':[]}
        self.assertEqual({k:v for k,v in response_start_receipt(hostile).items() if k.startswith('decoder_')},
            {'decoder_stage':'unknown','decoder_exception_type':'unknown','decoder_reason':'unknown'})

    async def test_first_attach_substep_survives_cleanup_and_durable_controller_projection(self):
        factory=response_runtime.build_native_meeting_responder
        cases=[(step,False) for step in ('source_offer','target_answer','source_answer',
            'target_ready','source_check','sender_install','initial_mute')]+[('source_offer',True),('sender_install',True)]
        for step,fail_cleanup in cases:
            with self.subTest(step=step,fail_cleanup=fail_cleanup),tempfile.TemporaryDirectory() as root:
                target,source=Page(step,fail_cleanup=fail_cleanup),Page(step)
                controller=startup_fixture.ResponseStartDiagnosticsTests().controller(root)
                controller.meeting.page=SimpleNamespace(frames=[target])
                def build(*args,**kwargs):
                    native=factory(*args,**kwargs)
                    native.rpc=AsyncMock();native.rpc.executable='synthetic-codex'
                    native.rpc.thread.return_value='synthetic-thread'
                    native.rpc.notifications=asyncio.Queue()
                    native.rpc.process=SimpleNamespace(returncode=None)
                    native.peer=AsyncMock();native.peer.page=source
                    native.peer.offer.return_value='synthetic-offer'
                    async def request(method,params):
                        if method=='account/read':return {'account':{'type':'chatgpt'}}
                        if method in {'thread/realtime/start','thread/realtime/stop'}:
                            kind='sdp' if method.endswith('/start') else 'closed'
                            await native.rpc.notifications.put({'method':'thread/realtime/'+kind,
                                'params':{'threadId':'synthetic-thread','sdp':'synthetic-answer'}})
                        return {}
                    native.rpc.request.side_effect=request
                    return native
                version=SimpleNamespace(returncode=0,
                    communicate=AsyncMock(return_value=(PINNED_CODEX_VERSION.encode(),b'')))
                with patch.object(response_runtime,'build_native_meeting_responder',build), \
                        patch('teams_transcription.b_native.asyncio.create_subprocess_exec',return_value=version):
                    self.assertFalse(await controller.start())
                expected={'phase':'native_output_attach','exception_type':'Error','output_step':step}
                if step=='target_answer':expected['target_document_policy']={'available':False}
                self.assertEqual(controller.status()['startup_error'],expected)
                self.assertEqual(response_start_receipt(controller.status()['startup_error']),expected)
                event=session_events(controller.session)[-1]
                self.assertEqual(event['type'],'response_unavailable')
                self.assertEqual(event['startup_error'],expected)
                self.assertEqual(controller.sink.cleanup_status,'unknown' if fail_cleanup else 'cleaned')
                self.assertFalse(controller.native.output.enabled)
                self.assertEqual(controller.sink._pending,fail_cleanup)
                self.assertNotIn(PRIVATE,json.dumps(event))
                if fail_cleanup:
                    self.assertEqual(controller.status()['state'],'stop_unknown')
                    with self.assertRaisesRegex(RuntimeError,'Owned response cleanup failed'):await controller.stop()
                else:await controller.stop()
                self.assertEqual(controller.status()['startup_error'],expected)

    async def test_relay_failure_remains_first_when_cleanup_also_fails(self):
        from teams_transcription.b_native_output import NativeTrackOutput
        source,target=Page('source_offer'),Page(None,fail_cleanup=True)
        sink=tx.BrowserTXSink(target,meeting_id='one')
        await sink.bind()
        output=NativeTrackOutput('one',sink)
        with self.assertRaises(OSError):await output.attach_live(source)
        self.assertEqual(output.startup_error,
            {'phase':'native_output_attach','exception_type':'Error','output_step':'source_offer'})
        self.assertEqual(sink.cleanup_status,'unknown')
        self.assertFalse(output.enabled)
        self.assertNotIn(PRIVATE,json.dumps(output.startup_error))

    def test_output_substep_is_a_fixed_optional_allowlist_not_exception_content(self):
        for value in (PRIVATE,[],{},None):
            with self.subTest(value=value):
                receipt=response_start_receipt({'phase':'native_output_attach',
                    'exception_type':'Error','output_step':value,'body':PRIVATE})
                self.assertEqual(receipt,{'phase':'native_output_attach','exception_type':'Error','output_step':'unknown'})
                self.assertNotIn(PRIVATE,json.dumps(receipt))
        self.assertEqual(response_start_receipt({'phase':'thread_start','exception_type':'Error',
            'output_step':'source_offer'}),{'phase':'thread_start','exception_type':'Error'})

